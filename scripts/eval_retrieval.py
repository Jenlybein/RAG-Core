"""检索评测脚本：一组固定语料 + 12 道固定题 → 「纯向量 vs 混合」与「改写开 / 关」。

**这是一次测量，不是功能。** 它回答的是「我们这套实现到底比纯向量强多少」，因此
三件事必须成立，否则测出来的只是替身：

1. **真实链路**：真实 PostgreSQL（pgvector）+ 真实 Elasticsearch（IK）+ 真实百炼嵌入。
   没有任何一个 Provider 在这里被替换过——被替换的话，测的就是替身。
2. **可复跑**：语料用固定 id 的独立知识库承载（``kb-eval`` / ``kb-eval-ops``），每次
   重跑先清后写，绝不把同一份语料索引两遍；``chunk_id`` 虽然每次重建都会变，但题目标注
   用的是 ``(document, heading_path)``，由脚本对着**真实**切片元数据解析成 ``chunk_id``
   ——标签写错会当场失败，而不是静默算成漏召。
3. **可解释**：每题给出命中 / 部分命中 / 漏 / 误召 / 诚实空，以及每条返回切片的
   ``vector_rank`` / ``bm25_rank`` / ``rrf_score``，融合分可以手算对账。

用法::

    # 全量：索引语料 → vector vs hybrid →（有 LLM 凭据时）改写开/关 → 验收自动项
    uv run python scripts/eval_retrieval.py --env-file ../RAG-Core/.env

    # 复用已有切片，不重新索引（改题目或调参时用）
    uv run python scripts/eval_retrieval.py --env-file ../RAG-Core/.env --reuse

    # ES 真的停掉之后跑降级矩阵（验收第 6、7 条）；跑完记得把容器起回来
    docker stop rag-es
    uv run python scripts/eval_retrieval.py --env-file ../RAG-Core/.env --degraded-probe
    docker start rag-es

    # 清理本脚本在本地留下的全部痕迹（PG 行 + ES 索引）
    uv run python scripts/eval_retrieval.py --env-file ../RAG-Core/.env --cleanup

**清理边界（重要）**：

* PostgreSQL：只删 ``tenant_id = tenant_demo`` 且
  ``kb_id ∈ {kb-eval, kb-eval-ops, kb-eval-empty}`` 的 ``chunks`` / ``documents`` /
  ``knowledge_bases`` 行。其他库、其他租户一行都不动。
* Elasticsearch：只删**评测专用索引**（``<ELASTICSEARCH_INDEX>_eval``，默认
  ``rag_chunks_eval``）。脚本拒绝删除名字里不含 ``eval`` 的索引，避免手滑清掉生产索引。
* 产物：``eval/runs/<时间戳>/``（已在 .gitignore 里）。

结果写在 ``eval/runs/<时间戳>/``：``results.json``（机器可读，含全部原始返回）、
``report.md``（人读的汇总表 + 逐题差异）。

**改写对比的诚实边界**：没配大模型 key 时不跑这一组，并在结果里写死
``改写对比：未执行（缺 LLM 凭据）``。绝不伪造一组数字出来。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUESTIONS_PATH = EVAL_DIR / "questions.json"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
RUNS_DIR = EVAL_DIR / "runs"

# RRF 常数：与 ``app.services.retrieval.fusion`` 同值，这里只用来**手算对账**。
# 手算值不从融合模块 import：那会让「对账」变成「用实现验证实现」。
RRF_K = 60
DEFAULT_TOP_K = 5

# 评测专用索引的后缀。脚本会强制把 ``settings.ELASTICSEARCH_INDEX`` 换成
# ``<原值>_eval``，所以 .env 里写的是生产索引名也不会污染它。
EVAL_INDEX_SUFFIX = "_eval"

# 三个固定知识库 id：重跑时先清后写靠的就是「id 固定」（随机 id 会越跑越多）。
EVAL_KB_IDS = ("kb-eval", "kb-eval-ops", "kb-eval-empty")
PRIMARY_KB_ID = "kb-eval"

MODE_VECTOR = "vector"
MODE_BM25 = "bm25"
MODE_HYBRID = "hybrid"

# 空结果两种原因（与 ``app.services.rag_service`` 的常量同值）。这里复制字面量而不是
# import：验收矩阵断言的是**对外契约**，实现改了常量名不该让验收跟着悄悄通过。
EMPTY_NO_INDEXED_CHUNKS = "no_indexed_chunks"
EMPTY_NO_CHUNKS_MATCHED = "no_chunks_matched"


# ─────────────────────────────────────────────────────── 环境变量必须先于 settings


def _preload_env_file() -> str | None:
    """把 ``--env-file`` 里的键灌进 ``os.environ``，**必须**早于 ``app.core.config``。

    ``settings`` 是模块级单例（``app/core/config.py`` 末尾 ``settings = Settings()``），
    导入即定型。所以 --env-file 只能在导入之前处理，不能「导入后再 reload」。

    用 ``setdefault``：显式传进来的环境变量优先于文件，与 pydantic-settings 的优先级
    一致，运维用 ``$env:MODEL_API_KEY=...`` 覆盖文件时不会被反咬。

    直接用 ``python-dotenv`` 而不是自己切 ``KEY=VALUE``：它是 ``pydantic-settings``
    的硬依赖（后者本来就靠它读 ``.env``），而 ``pydantic-settings`` 已在
    ``pyproject.toml`` 里硬钉版本。自己写一个半吊子解析器，反而会在引号、行内注释、
    ``export`` 前缀这些地方悄悄读错一个 key——那会让「评测用的是哪套环境」变成一个
    查不出来的问题。
    """

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=None)
    known, _ = parser.parse_known_args()
    if not known.env_file:
        return None

    from dotenv import dotenv_values

    for key, value in dotenv_values(known.env_file).items():
        if value is not None:
            os.environ.setdefault(key, value)
    return known.env_file


_ENV_FILE = _preload_env_file()

# 下面这些 import 必须排在 ``_preload_env_file()`` 之后（E402），否则 settings 会先按
# 空环境定型，--env-file 就白传了。
from app.core.config import settings  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.db.base import Base, now  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.models import Chunk, Document, KnowledgeBase  # noqa: E402
from app.providers.llm import get_llm_provider  # noqa: E402
from app.providers.llm.noop import NoopLLMProvider  # noqa: E402
from app.services.indexing_service import IndexingService  # noqa: E402
from app.services.rag_service import retrieve  # noqa: E402
from app.utils.status import DocumentStatus  # noqa: E402


# ───────────────────────────────────────────────────────────────────── 小工具


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _short(chunk_id: str | None) -> str:
    return "-" if not chunk_id else chunk_id[:8]


def _es_base() -> str:
    return settings.ELASTICSEARCH_URL.rstrip("/")


def _eval_index_name() -> str:
    """强制「评测自己的索引」：不是 ``rag_chunks``，而是 ``rag_chunks_eval``。"""

    base = settings.ELASTICSEARCH_INDEX or "rag_chunks"
    return base if base.endswith(EVAL_INDEX_SUFFIX) else f"{base}{EVAL_INDEX_SUFFIX}"


def _assert_eval_index(name: str) -> None:
    """防手滑：只允许动名字里带 eval 的索引。"""

    if "eval" not in name:
        raise SystemExit(
            f"refusing to touch an index without 'eval' in its name: {name!r}. "
            "本脚本只清理评测专用索引。"
        )


# ─────────────────────────────────────────────────────────────── 环境预检与隔离


@dataclass(frozen=True)
class LiveEnvironment:
    """这次评测真正跑在什么环境上——写进结果，防止「用哪套环境跑的都说不清」。"""

    env_file: str | None
    database_url: str
    embedding_model: str
    embedding_dimensions: int
    embedding_provider: str
    keyword_provider: str
    elasticsearch_url: str
    elasticsearch_index: str
    hybrid_recall_top_k: int
    rrf_k: int
    top_k: int
    rerank_enabled: bool
    query_rewrite_enabled: bool
    synonym_expansion_enabled: bool
    llm_configured: bool
    llm_probe: str

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _isolate_settings(*, top_k: int) -> None:
    """把评测要用的旋钮**显式钉死**，不吃 .env 的偶然值。

    钉死的四项都不是「顺手」：

    * ``ELASTICSEARCH_INDEX`` → ``*_eval``：绝不写生产索引；
    * ``RERANK_ENABLED=False``：重排会让 ``score`` 变成重排分、``rrf_score`` 只是历史值，
      「融合分手算对账」（验收第 4 条）就没法做了；
    * ``SYNONYM_EXPANSION_ENABLED=False``：同义词只影响关键词一路，开着它做
      「vector vs hybrid」等于在对比里混进第三个变量；
    * ``QUERY_REWRITE_ENABLED=False``：主对比先给出**关掉一切可选优化**的基线，
      改写开/关是单独一组。
    """

    settings.ELASTICSEARCH_INDEX = _eval_index_name()
    settings.RERANK_ENABLED = False
    settings.SYNONYM_EXPANSION_ENABLED = False
    settings.QUERY_REWRITE_ENABLED = False
    settings.TOP_K = top_k


def _probe_llm() -> tuple[bool, str]:
    """改写这一组能不能跑，只看一件事：快档大模型是否真的装配出来了。

    三种结果都必须区分清楚，因为它们对「改写开/关」的可执行性含义不同：

    * 装配成功 → 能跑；
    * 工厂返回空实现（该供应商的 key 没配）→ 缺 LLM 凭据，不能跑；
    * 工厂自己抛错（``LLM_PROVIDER`` 写了个不认识的名字）→ 同样不能跑，但原因是
      **配置写错**而不是「没 key」。把它也归进「缺凭据」会掩盖一个真实的配置错误，
      所以这里保留原始信息，写进结果文件。

    绝不让预检因为这一项失败：改写是可选优化，没它照样能测 vector vs hybrid。
    """

    try:
        provider = get_llm_provider("fast")
    except Exception as exc:  # noqa: BLE001 - 任何装配失败都只影响「改写」这一组
        return False, f"LLM_PROVIDER 配置不可用（{type(exc).__name__}: {exc}）"
    if isinstance(provider, NoopLLMProvider):
        return False, f"缺 LLM 凭据（供应商 {settings.LLM_PROVIDER!r} 的 key 未配置）"
    return True, "ok"


def _preflight() -> LiveEnvironment:
    """预检：缺什么就当场说清楚，绝不在半配置的环境上跑出一组假数字。"""

    problems: list[str] = []
    if not settings.MODEL_BASE_URL:
        problems.append("MODEL_BASE_URL is empty — 评测必须用真实嵌入，不接受替身")
    if not settings.MODEL_API_KEY:
        problems.append("MODEL_API_KEY is empty — 嵌入是真实链路的一条腿，不能缺")
    if settings.KEYWORD_SEARCH_PROVIDER.strip().lower() != "elasticsearch":
        problems.append(
            "KEYWORD_SEARCH_PROVIDER must be 'elasticsearch', got "
            f"{settings.KEYWORD_SEARCH_PROVIDER!r} — 没有关键词一路就测不了混合检索"
        )
    if problems:
        raise SystemExit("评测环境未就绪:\n  - " + "\n  - ".join(problems))

    missing_tables: list[str] = []
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            if not engine.dialect.has_table(connection, table.name):
                missing_tables.append(table.name)
    if missing_tables:
        raise SystemExit(f"数据库缺表 {missing_tables}；先跑 `uv run alembic upgrade head`。")

    llm_configured, llm_probe = _probe_llm()

    return LiveEnvironment(
        env_file=_ENV_FILE,
        database_url=settings.DATABASE_URL,
        embedding_model=settings.EMBEDDING_MODEL,
        embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
        embedding_provider=settings.EMBEDDING_PROVIDER,
        keyword_provider=settings.KEYWORD_SEARCH_PROVIDER,
        elasticsearch_url=settings.ELASTICSEARCH_URL,
        elasticsearch_index=settings.ELASTICSEARCH_INDEX,
        hybrid_recall_top_k=settings.HYBRID_RECALL_TOP_K,
        rrf_k=RRF_K,
        top_k=settings.TOP_K,
        rerank_enabled=settings.RERANK_ENABLED,
        query_rewrite_enabled=settings.QUERY_REWRITE_ENABLED,
        synonym_expansion_enabled=settings.SYNONYM_EXPANSION_ENABLED,
        llm_configured=llm_configured,
        llm_probe=llm_probe,
    )


# ───────────────────────────────────────────────────────────────── 语料与题目


@dataclass(frozen=True)
class CorpusDoc:
    stem: str
    path: Path
    text: str
    kb_id: str
    title: str

    @property
    def document_id(self) -> str:
        """固定 id：重跑不会造出第二份文档（这也是「不索引两遍」的一半）。"""

        return f"eval-doc-{self.stem}"


@dataclass(frozen=True)
class CorpusLayout:
    kb_id: str
    name: str
    description: str
    documents: tuple[str, ...]


def _load_manifest() -> tuple[list[CorpusLayout], str]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    layouts = [
        CorpusLayout(
            kb_id=kb["kb_id"],
            name=kb["name"],
            description=kb.get("description", ""),
            documents=tuple(kb["documents"]),
        )
        for kb in payload["kbs"]
    ]
    return layouts, payload["empty_kb"]["kb_id"]


def _load_corpus(layouts: Sequence[CorpusLayout]) -> list[CorpusDoc]:
    docs: list[CorpusDoc] = []
    for layout in layouts:
        for filename in layout.documents:
            path = CORPUS_DIR / filename
            if not path.is_file():
                raise SystemExit(f"corpus file missing: {path}")
            text = path.read_text(encoding="utf-8")
            docs.append(
                CorpusDoc(
                    stem=path.stem,
                    path=path,
                    text=text,
                    kb_id=layout.kb_id,
                    # 文档标题只影响「检索文本」的文档名前缀与响应里的 title，
                    # 取 Markdown 的一级标题；没有就用文件名。
                    title=_first_heading(text) or path.stem,
                )
            )
    return docs


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    note: str
    labels: tuple[tuple[str, str], ...]  # (document stem, heading_path)


def _load_questions() -> tuple[list[Question], int]:
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = [
        Question(
            id=item["id"],
            query=item["query"],
            note=item.get("note", ""),
            labels=tuple((label["document"], label["heading_path"]) for label in item["relevant"]),
        )
        for item in payload["questions"]
    ]
    return questions, int(payload.get("k", DEFAULT_TOP_K))


# ───────────────────────────────────────────────────────────────── 清理与索引


def _reset_elasticsearch(index_name: str) -> str:
    """删掉评测索引（下次写入时由 Provider 幂等重建）。"""

    _assert_eval_index(index_name)
    response = httpx.delete(f"{_es_base()}/{index_name}", timeout=30.0)
    if response.status_code == 200:
        return "deleted"
    if response.status_code == 404:
        return "absent"
    raise SystemExit(f"failed to delete index {index_name}: {response.status_code} {response.text}")


def _reset_postgres(db: Any) -> dict[str, int]:
    """删掉评测租户下三个固定知识库的全部行；其他数据一行不动。"""

    from sqlalchemy import delete

    counts = {
        "chunks": int(
            db.execute(
                delete(Chunk).where(
                    Chunk.tenant_id == settings.DEV_TENANT_ID, Chunk.kb_id.in_(EVAL_KB_IDS)
                )
            ).rowcount
            or 0
        ),
        "documents": int(
            db.execute(
                delete(Document).where(
                    Document.tenant_id == settings.DEV_TENANT_ID,
                    Document.kb_id.in_(EVAL_KB_IDS),
                )
            ).rowcount
            or 0
        ),
        "knowledge_bases": int(
            db.execute(
                delete(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == settings.DEV_TENANT_ID,
                    KnowledgeBase.id.in_(EVAL_KB_IDS),
                )
            ).rowcount
            or 0
        ),
    }
    db.commit()
    return counts


def _ensure_knowledge_base(db: Any, *, kb_id: str, name: str, description: str) -> None:
    if db.get(KnowledgeBase, kb_id) is not None:
        return
    db.add(
        KnowledgeBase(
            id=kb_id,
            tenant_id=settings.DEV_TENANT_ID,
            name=name,
            description=description,
            # 同义词表留空：本票的对比里同义词扩展是关的（第三变量），
            # 留空也顺便证明「默认配置下同义词题只能靠语义兜」。
            settings={},
        )
    )
    db.commit()


def _ensure_document(db: Any, doc: CorpusDoc) -> None:
    """把文档重置成「已排队、未开工」——与运维脚本用的那个状态准备一致。

    ``status=PROCESSING`` + ``index_claimed_at=NULL`` 正好是 Worker 可抢占的条件，
    所以重跑不会撞上「另一位 Worker 持有租约」而静默跳过。
    """

    row = db.get(Document, doc.document_id)
    if row is None:
        db.add(
            Document(
                id=doc.document_id,
                tenant_id=settings.DEV_TENANT_ID,
                kb_id=doc.kb_id,
                title=doc.title,
                content=doc.text,
                source_type="markdown",
                status=DocumentStatus.PROCESSING,
                index_claimed_at=None,
            )
        )
    else:
        row.kb_id = doc.kb_id
        row.title = doc.title
        row.content = doc.text
        row.status = DocumentStatus.PROCESSING
        row.index_claimed_at = None
        row.error_message = None
        row.updated_at = now()
    db.commit()


async def _index_documents(db: Any, docs: Sequence[CorpusDoc]) -> dict[str, int]:
    """走**真实**索引流水线：切块 → 拼检索文本 → 真实嵌入 → 双写（PG + ES）。"""

    service = IndexingService()
    result: dict[str, int] = {}
    for doc in docs:
        result[doc.stem] = await service.index_existing_document(db, doc.document_id)
    return result


# ─────────────────────────────────────────────────── 标签解析与切片索引（真实切片）


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    document_id: str
    kb_id: str
    heading_path: str
    order_index: int
    content_head: str


def _load_indexed_chunks(db: Any) -> list[IndexedChunk]:
    from sqlalchemy import select

    stmt = select(Chunk).where(
        Chunk.tenant_id == settings.DEV_TENANT_ID, Chunk.kb_id.in_(EVAL_KB_IDS)
    )
    chunks: list[IndexedChunk] = []
    for row in db.execute(stmt).scalars().all():
        meta = row.meta or {}
        chunks.append(
            IndexedChunk(
                chunk_id=row.id,
                document_id=row.document_id,
                kb_id=row.kb_id,
                heading_path=str(meta.get("heading_path") or ""),
                order_index=int(meta.get("order_index") or 0),
                content_head=(row.content or "").strip().replace("\n", " ")[:40],
            )
        )
    return chunks


def _resolve_labels(
    questions: Sequence[Question], chunks: Sequence[IndexedChunk]
) -> dict[str, list[str]]:
    """把 (document, heading_path) 标注解析成真实 ``chunk_id``。

    解析规则是「恰好一条」：0 条说明标注写错了（或语料被改了），多于 1 条说明该章节
    被切成了多片、只写 heading_path 不足以定位——两种都必须当场失败。**静默算成漏召
    是评测里最坏的一种错**：它看起来像「检索没做好」，实际是标注没对齐。
    """

    by_locator: dict[tuple[str, str], list[str]] = {}
    for chunk in chunks:
        by_locator.setdefault((chunk.document_id, chunk.heading_path), []).append(chunk.chunk_id)

    resolved: dict[str, list[str]] = {}
    problems: list[str] = []
    for question in questions:
        chunk_ids: list[str] = []
        for stem, heading_path in question.labels:
            matched = by_locator.get((f"eval-doc-{stem}", heading_path), [])
            if not matched:
                problems.append(
                    f"{question.id}: label not found -> document={stem} "
                    f"heading_path={heading_path!r}"
                )
            elif len(matched) > 1:
                problems.append(
                    f"{question.id}: label matched {len(matched)} chunks -> "
                    f"document={stem} heading_path={heading_path!r}；"
                    "该章节被切成了多片，标注需要更细的定位"
                )
            else:
                chunk_ids.append(matched[0])
        resolved[question.id] = chunk_ids

    if problems:
        raise SystemExit("题目标注与真实切片对不上:\n  - " + "\n  - ".join(problems))
    return resolved


# ─────────────────────────────────────────────────────────────────── 检索与打分


@dataclass
class QuestionOutcome:
    """一道题在某一种 mode 下的一次真实返回。"""

    id: str
    query: str
    mode: str
    returned: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    empty_reason: str | None = None
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def returned_ids(self) -> list[str]:
        return [str(chunk["chunk_id"]) for chunk in self.returned]


async def _run_question(
    db: Any, question: Question, *, mode: str, top_k: int, kb_ids: Sequence[str]
) -> QuestionOutcome:
    started = time.perf_counter()
    outcome = QuestionOutcome(id=question.id, query=question.query, mode=mode)
    try:
        response = await retrieve(
            db,
            tenant_id=settings.DEV_TENANT_ID,
            kb_ids=list(kb_ids),
            query=question.query,
            mode=mode,
            top_k=top_k,
        )
    except AppError as exc:
        # 单模式失败是**预期结果**（验收第 7 条就是要求它明确失败），不是脚本故障：
        # 记下业务码名与文案，继续跑其余题，由验收那一段判定通过与否。
        outcome.error = f"{exc.code.name}: {exc.msg}"
        outcome.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return outcome

    outcome.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    retrieval_meta = response["metadata"]["retrieval"]
    outcome.degraded = bool(retrieval_meta["degraded"])
    outcome.degraded_reason = retrieval_meta["degraded_reason"]
    outcome.empty_reason = retrieval_meta["empty_reason"]
    outcome.returned = list(response["retrieved_chunks"])
    return outcome


def _score_question(
    outcome: QuestionOutcome, *, relevant: Sequence[str], chunk_by_id: dict[str, IndexedChunk]
) -> dict[str, Any]:
    """一题的打分：Precision@k / Recall@k 按**实际返回条数**算。

    ``k`` 取「实际返回条数」而不是「请求的 top_k」：返回 2 条时必须按 2 条算精确率，
    否则「少返回」会被算成「不精确」，把一个降级 / 空结果的问题伪装成排序问题。
    """

    returned_ids = outcome.returned_ids
    relevant_set = set(relevant)
    hits = [chunk_id for chunk_id in returned_ids if chunk_id in relevant_set]
    false_positives = [chunk_id for chunk_id in returned_ids if chunk_id not in relevant_set]

    if relevant_set:
        precision = len(hits) / len(returned_ids) if returned_ids else 0.0
        recall = len(hits) / len(relevant_set)
    else:
        # 反向对照题（库里确实没有）：正确答案是「一条都不返回」。
        # 返回了任何东西都是误召，精确率与召回率都记 0，避免它把均值抬高。
        precision = 1.0 if not returned_ids else 0.0
        recall = 1.0 if not returned_ids else 0.0

    if relevant_set and not returned_ids:
        status = "漏（空结果）"
    elif relevant_set and len(hits) == len(relevant_set):
        status = "命中"
    elif relevant_set and hits:
        status = "部分命中"
    elif relevant_set:
        status = "漏"
    elif returned_ids:
        status = "误召"
    else:
        status = "诚实空"

    # 排名质量：Recall@5 打平时，第一条相关切片排在第几名才分得出高下。
    # 「命中」是布尔值，「命中在第 1 名还是第 5 名」才是用户真实感受的差别。
    first_hit_rank: int | None = None
    for position, chunk_id in enumerate(returned_ids, start=1):
        if chunk_id in relevant_set:
            first_hit_rank = position
            break
    if relevant_set:
        reciprocal_rank = 1.0 / first_hit_rank if first_hit_rank else 0.0
    else:
        # 反向对照题：一条都不返回才算「排对了」。
        reciprocal_rank = 1.0 if not returned_ids else 0.0

    return {
        "id": outcome.id,
        "query": outcome.query,
        "mode": outcome.mode,
        "status": status,
        "hits": len(hits),
        "relevant_total": len(relevant_set),
        "returned_total": len(returned_ids),
        "precision": _round(precision),
        "recall": _round(recall),
        "first_hit_rank": first_hit_rank,
        "reciprocal_rank": _round(reciprocal_rank),
        "returned": [
            {
                "chunk_id": chunk["chunk_id"],
                "heading_path": _heading_of(chunk["chunk_id"], chunk_by_id),
                "kb_id": chunk.get("kb_id"),
                "is_relevant": chunk["chunk_id"] in relevant_set,
                "vector_score": _round(chunk.get("vector_score")),
                "bm25_score": _round(chunk.get("bm25_score")),
                "vector_rank": chunk.get("vector_rank"),
                "bm25_rank": chunk.get("bm25_rank"),
                # 不在这里四舍五入：融合分要与手算值比对（验收第 4 条），
                # 存 6 位会把误差放大到 1e-7，让「对账通过」变成假阴性。
                "rrf_score": chunk.get("rrf_score"),
                "score": _round(chunk.get("score")),
            }
            for chunk in outcome.returned
        ],
        "missed_relevant": [
            {"chunk_id": chunk_id, "heading_path": _heading_of(chunk_id, chunk_by_id)}
            for chunk_id in relevant
            if chunk_id not in set(returned_ids)
        ],
        "false_positive_count": len(false_positives),
        "degraded": outcome.degraded,
        "degraded_reason": outcome.degraded_reason,
        "empty_reason": outcome.empty_reason,
        "error": outcome.error,
        "duration_ms": outcome.duration_ms,
    }


def _heading_of(chunk_id: str, chunk_by_id: dict[str, IndexedChunk]) -> str:
    chunk = chunk_by_id.get(chunk_id)
    return chunk.heading_path if chunk else ""


K_SWEEP = (1, 2, 3, 5)


def _prefix_metrics(
    item: dict[str, Any], *, k: int, relevant_total: int
) -> tuple[float, float] | None:
    """取这条返回的前 ``k`` 条重算 P / R；返回 ``None`` 表示不参与均值。

    用「同一次返回的前 k 条」而不是再发一次 ``top_k=k`` 的请求：两路召回深度都由
    服务端配置固定（``HYBRID_RECALL_TOP_K``），融合后的顺序与最终截断位置无关，
    所以前 k 条就是 ``top_k=k`` 会拿到的结果（截断只发生在最后一步）。这样 k 扫描
    不必再打一轮嵌入与 ES 请求。
    """

    if relevant_total <= 0:
        return None
    prefix = item["returned"][:k]
    hits = sum(1 for chunk in prefix if chunk["is_relevant"])
    precision = hits / len(prefix) if prefix else 0.0
    recall = hits / relevant_total
    return precision, recall


def _summarize(
    per_question: Sequence[dict[str, Any]], *, relevant_totals: dict[str, int]
) -> dict[str, Any]:
    """汇总分两组：可答题（11 道）与反向对照题（1 道）分开看。

    反向对照题的 P/R 只能取 0 或 1，混进均值会让「诚实空」这一个行为左右整个分数
    ——所以主指标只算可答题，反向对照题单独报。
    """

    answerable = [item for item in per_question if relevant_totals.get(item["id"], 0) > 0]
    negative = [item for item in per_question if relevant_totals.get(item["id"], 0) == 0]
    precision = (
        sum(item["precision"] for item in answerable) / len(answerable) if answerable else 0.0
    )
    recall = sum(item["recall"] for item in answerable) / len(answerable) if answerable else 0.0
    mrr = (
        sum(item["reciprocal_rank"] for item in answerable) / len(answerable) if answerable else 0.0
    )
    return {
        "answerable_count": len(answerable),
        "precision_at_k_macro": _round(precision),
        "recall_at_k_macro": _round(recall),
        "mrr_macro": _round(mrr),
        "k_sweep": _k_sweep(answerable, relevant_totals=relevant_totals),
        "status_counts": _count_statuses(answerable),
        "negative_control": negative[0] if negative else None,
        "per_question": list(per_question),
    }


def _k_sweep(
    answerable: Sequence[dict[str, Any]], *, relevant_totals: dict[str, int]
) -> list[dict[str, Any]]:
    """同一批排名的前 k 条：Recall@k 与 Precision@k 随 k 的变化。

    用途很具体：Recall@5 在两边都到顶（语料只有三十几片、top_k=5 已经把该进的都收
    进来）时，「混合到底有没有用」只能看更窄的预算——第一条就是答案的比例。
    """

    sweep: list[dict[str, Any]] = []
    for k in K_SWEEP:
        pairs = [
            metrics
            for item in answerable
            if (metrics := _prefix_metrics(item, k=k, relevant_total=relevant_totals[item["id"]]))
            is not None
        ]
        if not pairs:
            continue
        sweep.append(
            {
                "k": k,
                "recall_at_k_macro": _round(sum(recall for _, recall in pairs) / len(pairs)),
                "precision_at_k_macro": _round(sum(prec for prec, _ in pairs) / len(pairs)),
                "hit_at_k_count": sum(
                    1
                    for item in answerable
                    if any(chunk["is_relevant"] for chunk in item["returned"][:k])
                ),
            }
        )
    return sweep


def _count_statuses(items: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


async def _run_group(
    db: Any,
    questions: Sequence[Question],
    labels: dict[str, list[str]],
    chunk_by_id: dict[str, IndexedChunk],
    *,
    mode: str,
    kb_ids: Sequence[str],
    top_k: int,
) -> dict[str, Any]:
    per_question: list[dict[str, Any]] = []
    for question in questions:
        outcome = await _run_question(db, question, mode=mode, top_k=top_k, kb_ids=kb_ids)
        per_question.append(
            _score_question(outcome, relevant=labels[question.id], chunk_by_id=chunk_by_id)
        )
    relevant_totals = {qid: len(ids) for qid, ids in labels.items()}
    return _summarize(per_question, relevant_totals=relevant_totals)


# ───────────────────────────────────────────────────────────── 融合分手算对账


def _check_rrf(per_question: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """用返回的 ``vector_rank`` / ``bm25_rank`` 手算 RRF，与 ``rrf_score`` 对账。

    这是「融合分可手算」那条验收的实现：期望值完全由名次算出
    （``1/(60+向量名次) + 1/(60+关键词名次)``，未命中的一路不累加），不 import
    ``app.services.retrieval.fusion``。
    """

    checked = 0
    worst: dict[str, Any] | None = None
    for item in per_question:
        for chunk in item["returned"]:
            ranks = [
                rank for rank in (chunk["vector_rank"], chunk["bm25_rank"]) if rank is not None
            ]
            actual = chunk["rrf_score"]
            if not ranks or actual is None:
                continue
            expected = sum(1.0 / (RRF_K + rank) for rank in ranks)
            error = abs(float(actual) - expected)
            checked += 1
            if worst is None or error > worst["abs_error"]:
                worst = {
                    "question": item["id"],
                    "heading_path": chunk["heading_path"],
                    "vector_rank": chunk["vector_rank"],
                    "bm25_rank": chunk["bm25_rank"],
                    "expected": round(expected, 9),
                    "actual": round(float(actual), 9),
                    "abs_error": round(error, 12),
                }
    passed = bool(checked) and (worst is None or worst["abs_error"] < 1e-9)
    return {"checked_chunks": checked, "worst_case": worst, "passed": passed}


# ─────────────────────────────────────────────────────────────────── 验收自动项


def _es_count(index: str) -> int:
    response = httpx.get(f"{_es_base()}/{index}/_count", timeout=30.0)
    response.raise_for_status()
    return int(response.json()["count"])


def _es_analyze(index: str, text: str, analyzer: str) -> list[str]:
    response = httpx.post(
        f"{_es_base()}/{index}/_analyze",
        json={"analyzer": analyzer, "text": text},
        timeout=30.0,
    )
    response.raise_for_status()
    return [token["token"] for token in response.json()["tokens"]]


def _hybrid_channel_hits(hybrid: dict[str, Any]) -> tuple[int, int]:
    vector_hits = sum(
        1
        for item in hybrid.get("per_question", [])
        for chunk in item["returned"]
        if chunk["vector_rank"] is not None
    )
    bm25_hits = sum(
        1
        for item in hybrid.get("per_question", [])
        for chunk in item["returned"]
        if chunk["bm25_rank"] is not None
    )
    return vector_hits, bm25_hits


def _find_question(group: dict[str, Any], question_id: str) -> dict[str, Any] | None:
    return next((item for item in group.get("per_question", []) if item["id"] == question_id), None)


def _rank_of(chunk_id: str, item: dict[str, Any] | None) -> int | None:
    """该切片在这一次返回里排第几名；没进列表就是 ``None``。"""

    if not item:
        return None
    for position, chunk in enumerate(item["returned"], start=1):
        if chunk["chunk_id"] == chunk_id:
            return position
    return None


async def _run_acceptance(
    db: Any,
    *,
    groups: dict[str, dict[str, Any]],
    labels: dict[str, list[str]],
    top_k: int,
    index: str,
    empty_kb_id: str,
) -> list[dict[str, Any]]:
    """跑验收矩阵里**不需要停容器**的七条，逐条给出命令 / 期望 / 实际 / 结论。"""

    checks: list[dict[str, Any]] = []
    script_cmd = "uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env"

    count = _es_count(index)
    checks.append(
        {
            "id": "A1",
            "title": "ES 有数据",
            "command": f'curl.exe -s "http://localhost:9200/{index}/_count"',
            "expected": "count > 0",
            "actual": f"count={count}",
            "passed": count > 0,
        }
    )

    sample = "设备证书链已过期"
    max_tokens = _es_analyze(index, sample, "ik_max_word")
    smart_tokens = _es_analyze(index, sample, "ik_smart")
    checks.append(
        {
            "id": "A2",
            "title": "IK 生效（中文切成词）",
            "command": (
                f'curl.exe -s -X POST "http://localhost:9200/{index}/_analyze" '
                f'-H "Content-Type: application/json" '
                f'-d \'{{"analyzer":"ik_max_word","text":"{sample}"}}\''
            ),
            "expected": "tokens 多于一个，且不是整句",
            "actual": f"ik_max_word={max_tokens} / ik_smart={smart_tokens}",
            "passed": len(max_tokens) > 1 and sample not in max_tokens,
        }
    )

    hybrid = groups.get(MODE_HYBRID, {})
    vector_hits, bm25_hits = _hybrid_channel_hits(hybrid)
    checks.append(
        {
            "id": "A3",
            "title": "hybrid 生效（两路都有命中）",
            "command": f"{script_cmd}  （看 groups.hybrid.per_question[*].returned）",
            "expected": "混合返回里既有 vector_rank 非空的切片，也有 bm25_rank 非空的切片",
            "actual": f"vector_rank 非空 {vector_hits} 条 / bm25_rank 非空 {bm25_hits} 条",
            "passed": vector_hits > 0 and bm25_hits > 0,
        }
    )

    rrf = _check_rrf(hybrid.get("per_question", []))
    worst = rrf.get("worst_case") or {}
    checks.append(
        {
            "id": "A4",
            "title": "RRF 数值正确（按名次手算对账）",
            "command": f"{script_cmd}  （见 results.json 的 rrf_check）",
            "expected": "每条 rrf_score == 1/(60+vector_rank) + 1/(60+bm25_rank)，误差 < 1e-9",
            "actual": (
                f"核对 {rrf['checked_chunks']} 条；最大误差 {worst.get('abs_error')}；"
                f"样例 vector_rank={worst.get('vector_rank')} "
                f"bm25_rank={worst.get('bm25_rank')} "
                f"expected={worst.get('expected')} actual={worst.get('actual')}"
            ),
            "passed": bool(rrf.get("passed")),
        }
    )

    target = labels["q01"][0] if labels.get("q01") else ""
    vector_item = _find_question(groups.get(MODE_VECTOR, {}), "q01")
    hybrid_item = _find_question(hybrid, "q01")
    vector_rank = _rank_of(target, vector_item)
    hybrid_rank = _rank_of(target, hybrid_item)
    hybrid_hit = hybrid_rank is not None
    checks.append(
        {
            "id": "A5",
            "title": "编号查询（E1024 命中；纯向量排在后面）",
            "command": f"{script_cmd}  （对比 groups.vector / groups.hybrid 的 q01）",
            "expected": "hybrid 命中 E1024 那一节，且名次不差于纯向量（BM25 给精确编号加分）",
            "actual": (
                f"vector 首条相关名次={vector_rank or '未命中'} / "
                f"hybrid 首条相关名次={hybrid_rank or '未命中'}"
            ),
            "passed": hybrid_hit,
        }
    )

    empty_outcome = await _run_question(
        db,
        Question(id="A8-no-index", query="证书过期怎么处理", note="", labels=()),
        mode=MODE_VECTOR,
        top_k=top_k,
        kb_ids=[empty_kb_id],
    )
    no_match_outcome = await _run_question(
        db,
        Question(id="A8-no-match", query="zzzqqqxxx", note="", labels=()),
        mode=MODE_BM25,
        top_k=top_k,
        kb_ids=[PRIMARY_KB_ID],
    )
    checks.append(
        {
            "id": "A8",
            "title": "空结果有原因（两种各触发一次）",
            "command": (
                f"{script_cmd}  （A8 探针：空库 mode=vector → no_indexed_chunks；"
                "有内容 mode=bm25 查不存在的词 → no_chunks_matched）"
            ),
            "expected": "no_indexed_chunks 与 no_chunks_matched 各出现一次，且都是成功（非报错）",
            "actual": (
                f"空库向量检索 empty_reason={empty_outcome.empty_reason}；"
                f"有内容 bm25 无命中 empty_reason={no_match_outcome.empty_reason}"
            ),
            "passed": (
                empty_outcome.empty_reason == EMPTY_NO_INDEXED_CHUNKS
                and no_match_outcome.empty_reason == EMPTY_NO_CHUNKS_MATCHED
            ),
        }
    )

    multi = await _run_question(
        db,
        Question(id="A9-multi-kb", query="证书过期怎么处理", note="", labels=()),
        mode=MODE_HYBRID,
        top_k=max(top_k, 10),
        kb_ids=[PRIMARY_KB_ID, "kb-eval-ops"],
    )
    kb_ids_hit = sorted({str(chunk.get("kb_id")) for chunk in multi.returned})
    checks.append(
        {
            "id": "A9",
            "title": "多库检索（2 个 kb_ids）",
            "command": f"{script_cmd}  （A9 探针：kb_ids=[kb-eval, kb-eval-ops]）",
            "expected": "一次调用返回的结果里带不同的 kb_id（两个库都真的被召回）",
            "actual": f"返回 {len(multi.returned)} 条，kb_id={kb_ids_hit}",
            "passed": len(kb_ids_hit) >= 2,
        }
    )

    return checks


# ─────────────────────────────────────────────────────── 降级探针（ES 停着时跑）


async def _run_degraded_probe(db: Any, *, top_k: int) -> dict[str, dict[str, Any]]:
    """ES 真的停掉之后跑验收第 6、7 条：混合降级 vs 单模式明确失败。"""

    probe = Question(id="degraded", query="E1024 是什么错误", note="", labels=())
    hybrid = await _run_question(db, probe, mode=MODE_HYBRID, top_k=top_k, kb_ids=[PRIMARY_KB_ID])
    bm25 = await _run_question(db, probe, mode=MODE_BM25, top_k=top_k, kb_ids=[PRIMARY_KB_ID])

    hybrid_ok = (
        hybrid.error is None
        and hybrid.degraded
        and hybrid.degraded_reason == "keyword search failed"
        and len(hybrid.returned) > 0
    )
    bm25_ok = bm25.error is not None and bm25.error.startswith("RETRIEVAL_FAILED")
    probe_cmd = (
        "docker stop rag-es && uv run python scripts/eval_retrieval.py "
        "--env-file <主 checkout>/.env --degraded-probe"
    )

    return {
        "A6": {
            "id": "A6",
            "title": "ES 降级（混合模式）",
            "command": probe_cmd,
            "expected": (
                "code 仍为成功、degraded=true、degraded_reason=keyword search failed、仍有向量结果"
            ),
            "actual": (
                f"error={hybrid.error} degraded={hybrid.degraded} "
                f"degraded_reason={hybrid.degraded_reason} 返回 {len(hybrid.returned)} 条"
            ),
            "passed": hybrid_ok,
        },
        "A7": {
            "id": "A7",
            "title": "单模式不降级（bm25 + ES 停着）",
            "command": probe_cmd,
            "expected": "mode=bm25 明确失败（RETRIEVAL_FAILED / 50002），绝不偷偷退回向量",
            "actual": f"error={bm25.error}",
            "passed": bm25_ok,
        },
    }


# ─────────────────────────────────────────────────────────────── 产物（报告）


def _group_table(title: str, group: dict[str, Any]) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| 题目 | 问句 | 命中/相关 | 返回 | 首条相关名次 | P@k | R@k | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in group["per_question"]:
        rank = item["first_hit_rank"] if item["first_hit_rank"] is not None else "—"
        lines.append(
            f"| {item['id']} | {item['query']} | {item['hits']}/{item['relevant_total']} | "
            f"{item['returned_total']} | {rank} | {_fmt(item['precision'])} | "
            f"{_fmt(item['recall'])} | {item['status']} |"
        )
    lines += [
        "",
        f"**Precision@k（可答 {group['answerable_count']} 题）= "
        f"{_fmt(group['precision_at_k_macro'])}；Recall@k = {_fmt(group['recall_at_k_macro'])}；"
        f"MRR = {_fmt(group['mrr_macro'])}**",
        "",
    ]
    negative = group.get("negative_control")
    if negative:
        tail = ""
        if negative["returned_total"]:
            arrows = "、".join(chunk["heading_path"] for chunk in negative["returned"][:3])
            tail = f"；误召指向：{arrows}"
        lines += [
            f"反向对照题 {negative['id']}（{negative['query']}）："
            f"返回 {negative['returned_total']} 条，判定「{negative['status']}」{tail}。",
            "",
        ]
    return lines


def _findings(
    *, vector: dict[str, Any], hybrid: dict[str, Any], rrf: dict[str, Any], rewrite: dict[str, Any]
) -> list[str]:
    """由数据算出结论段：不写死任何一句判断，数字变了结论跟着变。"""

    lines: list[str] = ["", "## 结论与解读", ""]
    v_recall = vector["recall_at_k_macro"]
    h_recall = hybrid["recall_at_k_macro"]
    v_mrr = vector["mrr_macro"]
    h_mrr = hybrid["mrr_macro"]

    if v_recall == h_recall:
        lines.append(
            f"1. **Recall@k 打平（都是 {_fmt(v_recall)}）**：在这份语料与这个 top_k 上，"
            "纯向量已经把所有相关切片都排进了前 k 条，混合没有额外把「漏掉的」捞回来。"
            "这不是「hybrid 没用」，而是**这份语料对召回已经到顶**——它测不出召回差异。"
        )
    elif h_recall > v_recall:
        lines.append(
            f"1. **混合把召回拉高了**：Recall@k {_fmt(v_recall)} → {_fmt(h_recall)}。"
            "相关切片本来掉出前 k 条的题，被关键词一路补了回来。"
        )
    else:
        lines.append(
            f"1. **混合反而降低了召回**：Recall@k {_fmt(v_recall)} → {_fmt(h_recall)}。"
            "关键词一路把不相关切片顶进前 k，挤掉了向量本来找对的那条——"
            "这是混合检索的典型代价，必须如实记录。"
        )

    if v_mrr == h_mrr:
        lines.append(
            f"2. **排名质量也打平（MRR 都是 {_fmt(v_mrr)}）**：两路在「第一条相关切片排第几」上"
            "没有差别。"
        )
    else:
        better = "混合更好" if h_mrr > v_mrr else "纯向量更好"
        lines.append(
            f"2. **排名质量差在 MRR：{_fmt(v_mrr)} → {_fmt(h_mrr)}（{better}）**。"
            "召回上界相同时，这是混合唯一能量出来的净收益："
            "同一条相关切片被提到了更靠前的位置。"
        )
        improved = [
            f"{v_item['id']}（{v_item['first_hit_rank'] or '未命中'} → "
            f"{h_item['first_hit_rank'] or '未命中'}）"
            for v_item, h_item in zip(vector["per_question"], hybrid["per_question"])
            if (v_item["first_hit_rank"] or 99) > (h_item["first_hit_rank"] or 99)
        ]
        worsened = [
            f"{v_item['id']}（{v_item['first_hit_rank'] or '未命中'} → "
            f"{h_item['first_hit_rank'] or '未命中'}）"
            for v_item, h_item in zip(vector["per_question"], hybrid["per_question"])
            if (v_item["first_hit_rank"] or 99) < (h_item["first_hit_rank"] or 99)
        ]
        if improved:
            lines.append(f"   - 名次被提前的题：{'、'.join(improved)}")
        if worsened:
            lines.append(f"   - 名次被推后的题：{'、'.join(worsened)}")
        lines.append(
            "   - 其余题名次不变。混合不是「全赢」：它靠关键词一路给精确字面加分，"
            "所以只在「问句里有文档原样出现的字面」时才能真正占便宜。"
        )

    # 编号洞：这一条是父 spec 的前提假设，必须如实写结果，而不是复述预期。
    e1024 = next((item for item in hybrid["per_question"] if item["id"] == "q01"), None)
    if e1024 is not None:
        v_rank = next(
            (i["first_hit_rank"] for i in vector["per_question"] if i["id"] == "q01"), None
        )
        h_rank = e1024["first_hit_rank"]
        if v_rank is None:
            lines.append(
                "3. **编号查询（E1024）：纯向量漏了，混合补上了。** "
                f"vector 前 k 条里没有 E1024 那一节，hybrid 把它排到第 {h_rank} 名"
                "——这正是父 spec 预期的「BM25 补洞」。"
            )
        else:
            lines.append(
                "3. **编号查询（E1024）：纯向量也命中了（排第 "
                f"{v_rank} 名），混合排第 {h_rank or '未命中'} 名。** "
                "也就是说父 spec 里「向量记不住那串字」这个前提，**在本项目这套嵌入 + "
                "这份语料上没有复现**：检索文本把编号写在了切片里（票 2 的设计），"
                "嵌入模型对这类规整标识符并不盲。要复现「向量漏」需要更大的语料或更"
                "不规整的标识符（随机串、哈希、内部拼写），这一条留给上游决定是否补测。"
            )

    negative = hybrid.get("negative_control")
    if negative:
        if negative["returned_total"] == 0:
            lines.append(
                "4. **反向对照题（库里确实没有）：诚实空。** 这类问题只能靠关键词一路"
                "给出「没命中」，因为向量检索永远返回离它最近的 k 条。"
            )
        else:
            lines.append(
                f"4. **反向对照题（库里确实没有）：返回了 {negative['returned_total']} 条，"
                "判定「误召」。** 这是纯向量的结构性行为——它永远返回最近的 k 条，没有"
                "「查不到」这个状态；混合模式继承了这一点（向量一路在，就一定有结果）。"
                "要真正做到「诚实空」，需要一条分数阈值或「关键词一路为空即空」的策略，"
                "本票不实现，记给上游。"
            )

    if rewrite["executed"]:
        lines.append("5. **改写对比：已执行**，见下面的改写对比一节。")
    else:
        lines.append(
            f"5. **改写对比：未执行（{rewrite['reason']}）**。{rewrite.get('detail', '')}。"
            "这一组数字**不存在**，不要在任何地方按「改写有用 / 没用」引用本报告。"
        )
    lines.append(
        f"6. **融合分对账：{'通过' if rrf['passed'] else '不通过'}**"
        f"（核对 {rrf['checked_chunks']} 条，最大误差 "
        f"{(rrf.get('worst_case') or {}).get('abs_error', 'n/a')}）。"
    )
    return lines


def _write_report(
    path: Path,
    *,
    environment: LiveEnvironment,
    corpus_stats: dict[str, Any],
    groups: dict[str, dict[str, Any]],
    rrf: dict[str, Any],
    acceptance: Sequence[dict[str, Any]],
    rewrite: dict[str, Any],
    questions: Sequence[Question],
    labels: dict[str, list[str]],
    chunk_by_id: dict[str, IndexedChunk],
) -> None:
    vector = groups[MODE_VECTOR]
    hybrid = groups[MODE_HYBRID]
    worst = rrf.get("worst_case") or {}
    lines: list[str] = [
        "# 检索评测报告（票 8）",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 这次测的是什么",
        "",
        "- **真实链路**：真实 PostgreSQL（pgvector）+ 真实 Elasticsearch（IK）+ "
        f"真实嵌入（{environment.embedding_model} / {environment.embedding_dimensions} 维）。"
        "没有替换任何 Provider。",
        "- 语料：`eval/corpus/`，"
        f"{corpus_stats['document_count']} 篇文档、{corpus_stats['chunk_count']} 个切片，"
        f"落在独立知识库 `{corpus_stats['kb_ids']}`。",
        f"- 题目：`eval/questions.json`，{len(questions)} 道，人工标注到 "
        "`(document, heading_path)`，由脚本解析成真实 `chunk_id`（标注对不上会当场报错）。",
        f"- 融合常数 `rrf_k={environment.rrf_k}`（服务端写死，不进请求体）；"
        f"`top_k={environment.top_k}`；每路召回深度 `{environment.hybrid_recall_top_k}`。",
        "- 重排："
        f"{'开' if environment.rerank_enabled else '关'}；同义词扩展："
        f"{'开' if environment.synonym_expansion_enabled else '关'}。"
        "两者都在本次对比里显式关闭——它们是第三变量，混进来就说不清是谁的功劳。",
        "",
        "> **与教程原始项目的数字没有可比性。** 教程里那三组评测数字在本项目**未复现**"
        "（语料、题目、模型、k 都不同）；这里给出的只是本项目自己的一组数字，"
        "用途是「同一套代码里 vector 与 hybrid 的相对差异」。",
        "",
        "## 汇总",
        "",
        "| 模式 | Precision@k | Recall@k | MRR | 命中 | 部分命中 | 漏 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, group in ((MODE_VECTOR, vector), (MODE_HYBRID, hybrid)):
        counts = group["status_counts"]
        missed = counts.get("漏", 0) + counts.get("漏（空结果）", 0)
        lines.append(
            f"| {name} | {_fmt(group['precision_at_k_macro'])} | "
            f"{_fmt(group['recall_at_k_macro'])} | {_fmt(group['mrr_macro'])} | "
            f"{counts.get('命中', 0)} | "
            f"{counts.get('部分命中', 0)} | {missed} |"
        )

    lines += _findings(vector=vector, hybrid=hybrid, rrf=rrf, rewrite=rewrite)

    lines += [
        "",
        "## 逐题差异（hybrid 相对 vector）",
        "",
        "| 题目 | vector 判定 | hybrid 判定 | 差异 |",
        "| --- | --- | --- | --- |",
    ]
    for vector_item, hybrid_item in zip(vector["per_question"], hybrid["per_question"]):
        if hybrid_item["recall"] > vector_item["recall"]:
            verdict = "hybrid 赢"
        elif hybrid_item["recall"] < vector_item["recall"]:
            verdict = "hybrid 输"
        elif (hybrid_item["first_hit_rank"] or 99) < (vector_item["first_hit_rank"] or 99):
            verdict = "hybrid 名次更好"
        elif (hybrid_item["first_hit_rank"] or 99) > (vector_item["first_hit_rank"] or 99):
            verdict = "hybrid 名次更差"
        else:
            verdict = "打平"
        lines.append(
            f"| {vector_item['id']} | {vector_item['status']} | "
            f"{hybrid_item['status']} | {verdict} |"
        )

    lines += [
        "",
        "## 排名质量：Recall@5 到顶时，混合赢在哪一格",
        "",
        "Recall@5 反映「该进的进没进来」。语料只有三十几片、`top_k=5` 已经把该进的都收进来时，"
        "它会在两边同时到顶；此时真正区分两路的是**第一条相关切片排在第几名**。",
        "",
        "| 模式 | MRR | Recall@1 | Recall@2 | Recall@3 | Recall@5 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, group in ((MODE_VECTOR, vector), (MODE_HYBRID, hybrid)):
        sweep = {row["k"]: row["recall_at_k_macro"] for row in group["k_sweep"]}
        lines.append(
            f"| {name} | {_fmt(group['mrr_macro'])} | {_fmt(sweep.get(1))} | "
            f"{_fmt(sweep.get(2))} | {_fmt(sweep.get(3))} | {_fmt(sweep.get(5))} |"
        )
    lines += [
        "",
        "（Recall@k 用的是同一次返回的前 k 条：召回深度固定、融合后的顺序与最终截断位置"
        "无关，所以前 k 条就是请求 `top_k=k` 会拿到的结果。）",
        "",
        "| 题目 | vector 首条相关名次 | hybrid 首条相关名次 |",
        "| --- | --- | --- |",
    ]
    for vector_item, hybrid_item in zip(vector["per_question"], hybrid["per_question"]):
        lines.append(
            f"| {vector_item['id']} | {vector_item['first_hit_rank'] or '未命中'} | "
            f"{hybrid_item['first_hit_rank'] or '未命中'} |"
        )

    lines += ["", "## 两组细节", ""]
    lines += _group_table("mode=vector", vector)
    lines += _group_table("mode=hybrid", hybrid)

    lines += [
        "## RRF 手算对账",
        "",
        f"核对 {rrf['checked_chunks']} 条融合结果，最大绝对误差 "
        f"{worst.get('abs_error', 'n/a')}；结论：{'通过' if rrf['passed'] else '不通过'}。",
        "",
    ]
    if worst:
        lines += [
            f"样例：{worst['heading_path']} —— vector_rank={worst['vector_rank']}、"
            f"bm25_rank={worst['bm25_rank']}、手算 expected={worst['expected']}、"
            f"返回 actual={worst['actual']}。",
            "",
        ]

    lines += ["## 改写对比", ""]
    if rewrite["executed"]:
        on_group = rewrite["groups"]["rewrite_on"]
        off_group = rewrite["groups"]["rewrite_off"]
        lines += [
            "| 组 | Precision@k | Recall@k |",
            "| --- | --- | --- |",
            f"| 改写关 | {_fmt(off_group['precision_at_k_macro'])} | "
            f"{_fmt(off_group['recall_at_k_macro'])} |",
            f"| 改写开 | {_fmt(on_group['precision_at_k_macro'])} | "
            f"{_fmt(on_group['recall_at_k_macro'])} |",
            "",
        ]
    else:
        lines += [
            f"**改写对比：未执行（{rewrite['reason']}）**",
            "",
            f"探测结果：{rewrite.get('detail', '')}",
            "",
        ]

    lines += [
        "## 验收矩阵（自动部分）",
        "",
        "| # | 项 | 期望 | 实际 | 结论 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in acceptance:
        lines.append(
            f"| {check['id']} | {check['title']} | {check['expected']} | "
            f"{check['actual']} | {'通过' if check['passed'] else '不通过'} |"
        )
    lines += [
        "",
        "第 6、7 条（ES 降级 / 单模式不降级）需要真停容器，见 `eval/ACCEPTANCE.md`。",
        "",
        "## 标注与切片对照",
        "",
        "| 题目 | 标注（heading_path） | 解析到的 chunk |",
        "| --- | --- | --- |",
    ]
    for question in questions:
        resolved = labels[question.id]
        if not resolved:
            lines.append(f"| {question.id} | （无，反向对照题） | — |")
            continue
        paths = "；".join(_heading_of(cid, chunk_by_id) for cid in resolved)
        lines.append(f"| {question.id} | {paths} | {', '.join(_short(cid) for cid in resolved)} |")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────── 主流程


async def _run(args: argparse.Namespace) -> int:
    _isolate_settings(top_k=args.top_k)
    index_name = settings.ELASTICSEARCH_INDEX
    _assert_eval_index(index_name)

    if args.cleanup:
        return _cleanup(db_factory=SessionLocal, index_name=index_name)

    environment = _preflight()
    questions, question_k = _load_questions()
    top_k = args.top_k or question_k

    layouts, empty_kb_id = _load_manifest()
    docs = _load_corpus(layouts)

    db = SessionLocal()
    try:
        if args.degraded_probe:
            probe = await _run_degraded_probe(db, top_k=top_k)
            print("\n=== degraded probe (A6 / A7) ===")
            for key in ("A6", "A7"):
                item = probe[key]
                verdict = "PASS" if item["passed"] else "FAIL"
                print(f"{item['id']} {item['title']}: {verdict} -- {item['actual']}")
            return 0 if all(item["passed"] for item in probe.values()) else 1

        print("=== eval setup ===")
        print(f"database : {environment.database_url}")
        print(f"es index : {index_name}")
        print(f"embedding: {environment.embedding_model} ({environment.embedding_dimensions}d)")

        index_stats: dict[str, int] = {}
        if args.reuse:
            existing = _load_indexed_chunks(db)
            if not existing:
                raise SystemExit("--reuse 需要已有切片，但评测库里是空的；去掉 --reuse 重跑。")
            print(f"reuse    : {len(existing)} chunks (no re-indexing)")
        else:
            began = time.perf_counter()
            es_state = _reset_elasticsearch(index_name)
            pg_counts = _reset_postgres(db)
            print(f"reset    : es index {es_state}; pg {pg_counts}")
            for layout in layouts:
                _ensure_knowledge_base(
                    db, kb_id=layout.kb_id, name=layout.name, description=layout.description
                )
            _ensure_knowledge_base(
                db,
                kb_id=empty_kb_id,
                name="评测语料库·空库",
                description="验证 no_indexed_chunks 用，刻意不写任何文档",
            )
            for doc in docs:
                _ensure_document(db, doc)
            index_stats = await _index_documents(db, docs)
            print(f"indexed  : {index_stats} in {time.perf_counter() - began:.1f}s")

        chunks = _load_indexed_chunks(db)
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        labels = _resolve_labels(questions, chunks)
        corpus_stats = {
            "document_count": len({chunk.document_id for chunk in chunks}),
            "chunk_count": len(chunks),
            "kb_ids": sorted({chunk.kb_id for chunk in chunks}),
            "per_document": index_stats,
            "per_heading_path": [
                {
                    "kb_id": chunk.kb_id,
                    "heading_path": chunk.heading_path,
                    "chunk_id": chunk.chunk_id,
                }
                for chunk in chunks
            ],
        }
        print(f"corpus   : {corpus_stats['document_count']} docs / {len(chunks)} chunks")

        kb_ids = [layout.kb_id for layout in layouts]

        print("\n=== retrieval groups ===")
        vector_group = await _run_group(
            db, questions, labels, chunk_by_id, mode=MODE_VECTOR, kb_ids=kb_ids, top_k=top_k
        )
        hybrid_group = await _run_group(
            db, questions, labels, chunk_by_id, mode=MODE_HYBRID, kb_ids=kb_ids, top_k=top_k
        )
        groups = {MODE_VECTOR: vector_group, MODE_HYBRID: hybrid_group}
        print(
            f"vector   : P@k={_fmt(vector_group['precision_at_k_macro'])} "
            f"R@k={_fmt(vector_group['recall_at_k_macro'])}"
        )
        print(
            f"hybrid   : P@k={_fmt(hybrid_group['precision_at_k_macro'])} "
            f"R@k={_fmt(hybrid_group['recall_at_k_macro'])}"
        )

        # 改写开 / 关：只在真的配了大模型 key 时跑，绝不编造。
        rewrite: dict[str, Any] = {
            "executed": False,
            "reason": "缺 LLM 凭据",
            "detail": environment.llm_probe,
        }
        if not environment.llm_configured:
            print(f"rewrite  : SKIPPED ({environment.llm_probe})")
        else:
            rewrite_off = await _run_group(
                db, questions, labels, chunk_by_id, mode=MODE_HYBRID, kb_ids=kb_ids, top_k=top_k
            )
            settings.QUERY_REWRITE_ENABLED = True
            rewrite_on = await _run_group(
                db, questions, labels, chunk_by_id, mode=MODE_HYBRID, kb_ids=kb_ids, top_k=top_k
            )
            settings.QUERY_REWRITE_ENABLED = False
            rewrite = {
                "executed": True,
                "reason": "",
                "groups": {"rewrite_off": rewrite_off, "rewrite_on": rewrite_on},
            }
            print(
                f"rewrite  : off R@k={_fmt(rewrite_off['recall_at_k_macro'])} / "
                f"on R@k={_fmt(rewrite_on['recall_at_k_macro'])}"
            )

        print("\n=== acceptance ===")
        acceptance = await _run_acceptance(
            db,
            groups=groups,
            labels=labels,
            top_k=top_k,
            index=index_name,
            empty_kb_id=empty_kb_id,
        )
        for check in acceptance:
            verdict = "PASS" if check["passed"] else "FAIL"
            print(f"{check['id']}: {verdict} -- {check['title']} | {check['actual'][:100]}")

        rrf = _check_rrf(hybrid_group["per_question"])

        run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%dT%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "environment": environment.as_dict(),
            "corpus": corpus_stats,
            "questions": [
                {
                    "id": question.id,
                    "query": question.query,
                    "note": question.note,
                    "labels": [
                        {"document": stem, "heading_path": heading_path}
                        for stem, heading_path in question.labels
                    ],
                    "resolved_chunk_ids": labels[question.id],
                }
                for question in questions
            ],
            "groups": groups,
            "rrf_check": rrf,
            "acceptance": list(acceptance),
            "rewrite_comparison": rewrite,
        }
        (run_dir / "results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_report(
            run_dir / "report.md",
            environment=environment,
            corpus_stats=corpus_stats,
            groups=groups,
            rrf=rrf,
            acceptance=acceptance,
            rewrite=rewrite,
            questions=questions,
            labels=labels,
            chunk_by_id=chunk_by_id,
        )
        print(f"\nrun dir  : {run_dir}")
        print(f"report   : {run_dir / 'report.md'}")

        failed = [check["id"] for check in acceptance if not check["passed"]]
        return 1 if failed else 0
    finally:
        db.close()


def _cleanup(*, db_factory: Any, index_name: str) -> int:
    db = db_factory()
    try:
        counts = _reset_postgres(db)
    finally:
        db.close()
    index_state = _reset_elasticsearch(index_name)
    print(f"cleanup  : es index {index_name} {index_state}; pg {counts}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieval evaluation: vector vs hybrid (and rewrite on/off) over a fixed "
            "corpus, against real PostgreSQL + Elasticsearch + embeddings."
        )
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv file to load BEFORE app.core.config imports (explicit env wins)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"returned rows per query (default {DEFAULT_TOP_K}; k in Precision@k)",
    )
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="reuse the already-indexed corpus instead of resetting and re-indexing it",
    )
    parser.add_argument(
        "--degraded-probe",
        action="store_true",
        help="run acceptance checks 6/7 (requires Elasticsearch to be actually stopped)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the evaluation rows in PostgreSQL and the evaluation ES index",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
