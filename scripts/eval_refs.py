"""引用语料跑批骨架：8 道定向题 → 四类机械可核验的读数（票 A3 / #145）。

**它是什么**：一条命令，把 ``eval/refs-questions.json`` 的 8 道题跑一遍**今天**的检索链路，
产出四类指标：停止原因分布、引用回溯率、延迟、模型调用次数。它**不是**效果验收——
对照结论由 C6 在 A2 / C3 落地之后出（#138 §Further Notes），本脚本只搭台子。

**为什么要有「骨架」这个说法**：票里的「开 / 关对照」两侧在 A2（图谱注入）与 C3（Research
引用块）落地之前并不存在。此刻把 ``--graph on`` 跑一遍，只会得到与 ``off`` 一模一样的结果。
所以两侧都做成了参数位，边没接上时报告里印
``capability-not-implemented``——**绝不伪造一组相同的数字**装作对比做过了（票里的范围
澄清评论就是为这条写的）。等 A2 / C3 落地，同一个脚本、同一组参数就是真对照。

**三条诚实约束**（它们比指标本身更重要）：

1. **默认不调用真实模型**。缺省 ``--source dry-run``：不连库、不发网络、不烧一次嵌入。
   要钱的两件事各自需要显式参数——``--source live``（真实链路，每题一次查询嵌入）与
   ``--allow-model-calls``（允许 Research 一类生成调用，今天为 0）。开跑前先打印
   **预计调用次数**，跑完再打印**实际次数**，两个数都进报告。
2. **无数据库 / 无网络可跑**。``dry-run`` 用一个不返回任何结果的检索器把整条批处理流程
   （读题 → 统计 → 渲染 → 落盘）走一遍，所以「报告结构对不对」不需要数据库就能验证。
   ``--fixtures FILE`` 则回放一份固定结果文件——C6 把真实抓取存成文件后，离线也能重算。
3. **引用回溯率可机械复算**。给一条引用（文档 + 节路径 + 片段），能还原到语料原文：
   ``uv run python scripts/eval_refs.py verify-citation --document refund_policy \
--section 3 --snippet "银行卡一般 3 个工作日"``。同一个函数 ``resolve_citation`` 既服务
   CLI，也服务批处理里的逐条判定——两处口径不可能分叉。

**为什么四类指标是这四个**：停止原因分布回答「这次到底为什么停」（Research 的七种原因 /
今天链路的等价终止形态）；引用回溯率回答「每个引用能不能还原到库里的原文」——它只做
可回溯、**不做答案质量**（#138 Out of Scope）；延迟与模型调用次数回答「成本多少」。

用法::

    # 空跑自检：不连库、不发网络，产出结构正确的报告（默认就是它）
    uv run python scripts/eval_refs.py

    # 真实链路（要库、要嵌入凭据；每题一次查询嵌入）
    uv run python scripts/eval_refs.py --source live --allow-model-calls \
        --kb-ids kb-refs

    # 回放一份固定结果文件（C6 存的抓取结果）
    uv run python scripts/eval_refs.py --source fixture --fixtures /tmp/refs.json

    # 机械复算一条引用
    uv run python scripts/eval_refs.py verify-citation --document points_policy \
        --section 2.2 --snippet "银卡及以上时，200 元的固定上限提高到 500 元"

    # 冻结资产自检：eval/corpus 与 eval/baseline 有没有被改过
    uv run python scripts/eval_refs.py check-frozen
    # 确需变更时重新生成哈希清单（变更本身要能说清为什么）
    uv run python scripts/eval_refs.py freeze --write

产物写在 ``eval/refs-runs/<时间戳>/``：``results.json``（机器可读，含逐题原始返回）与
``report.md``（人读的汇总表）。该目录已在 ``.gitignore`` 里——读数留在本地，结论由 C6 出。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 以 ``python scripts/eval_refs.py`` 方式运行时 ``sys.path[0]`` 是 ``scripts/``；
    # ``scripts`` 是没有 ``__init__.py`` 的命名空间包，靠这行才能 ``from scripts...``。
    sys.path.insert(0, str(REPO_ROOT))

EVAL_DIR = REPO_ROOT / "eval"
REFS_CORPUS_DIR = EVAL_DIR / "corpus-refs"
REFS_MANIFEST = REFS_CORPUS_DIR / "manifest.json"
REFS_QUESTIONS = EVAL_DIR / "refs-questions.json"
FROZEN_MANIFEST = EVAL_DIR / "frozen-assets.sha256.json"
DEFAULT_RUNS_DIR = EVAL_DIR / "refs-runs"

#: 被冻结的两个目录（票里的守护对象）：**本票一个字都没动**，哈希清单钉住这件事。
#: 用相对仓库根的 posix 路径，清单因此与机器无关、与 ``core.autocrlf`` 无关
#: （哈希取的是把 CRLF 折成 LF 之后的字节）。
FROZEN_DIRS = ("eval/corpus", "eval/baseline")

#: Research 的七种固定停止原因（#138 §C）。顺序固定，报告 diff 才读得懂。
STOP_REASONS = (
    "evidence_complete",
    "no_new_evidence",
    "max_rounds",
    "duplicate_query",
    "timeout",
    "budget_exhausted",
    "degraded",
)

#: 「能力还没接上」的唯一写法。报告与 results.json 共用这一个字面量：换个说法
#: （比如 ``not-supported``）就等于让下游的 grep 失效。
CAPABILITY_NOT_IMPLEMENTED = "capability-not-implemented"

#: 三类数据来源。``dry-run`` 是缺省——**默认不花钱**这条纪律落在默认值上，
#: 而不是落在某个人记得加参数上。
SOURCE_DRY_RUN = "dry-run"
SOURCE_FIXTURE = "fixture"
SOURCE_LIVE = "live"
SOURCE_ORDER = (SOURCE_DRY_RUN, SOURCE_FIXTURE, SOURCE_LIVE)

#: 摘录长度：报告里只印片段（原文走 ``verify-citation`` 复算），不进表格。
SNIPPET_CHARS = 60

#: 一个节级交付单元最少要有多少字才算「一段材料」。低于这个长度的命中仍计入
#: 引用回溯率，但不参与覆盖读数——避免把「标题恰好撞上」当成覆盖。
MIN_MATERIAL_CHARS = 20

DEFAULT_TOP_K = 5

#: 子命令名（``main`` 用它判断「用户是给了子命令，还是直接给参数」）。
COMMANDS = ("run", "verify-citation", "check-frozen", "freeze")

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<number>\d+(?:\.\d+)?)\.?\s+(?P<title>.+?)\s*$")
_WS_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────── 语料与题库


def load_manifest(root: Path | None = None) -> dict[str, Any]:
    """读引用语料的清单（库布局 + 文档 + 节号 + 引用边清单）。"""

    path = (root or REFS_CORPUS_DIR) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_questions(root: Path | None = None) -> dict[str, Any]:
    """读定向题集。只解析，不校验——校验在 ``tests/test_eval_refs.py`` 里。"""

    path = (root or EVAL_DIR) / "refs-questions.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    """折叠空白（含全角空格以外的所有空白）——片段比对**不做**去标点：
    标点也是原文的一部分，改了它就不再是「还原到原文」。"""

    return _WS_RE.sub(" ", text).strip()


@dataclass(frozen=True)
class Section:
    """语料里的一节：节号、标题、原文（含标题行）。"""

    number: str
    title: str
    level: int
    text: str


def split_sections(text: str) -> dict[str, Section]:
    """按标题行把一篇文档拆成「节号 → 节」。

    节号来自标题自身的编号前缀（``## 3. 退款到账与凭证`` → ``3``；``### 2.2 抵扣上限``
    → ``2.2``），**不靠顺序推断**：顺序一改、加一节，推断出来的节号就全错，而
    「详见《X》第 3 节」这条边会静默指到别处去。

    一节的正文止于下一个**层级不高于它**的标题——所以 ``## 2.`` 的正文不含 ``### 2.1``
    的内容，而 ``### 2.1`` 的正文是它自己那一段。片段复算按这个边界判，跨节拼接的
    内容会如实判为「不在这一节里」。
    """

    lines = text.splitlines()
    # 边界按**所有**标题算，节号只取带编号的那些：没有编号的标题（比如文档的一级标题）
    # 不构成一节，但它仍然要能结束上一节——否则 ``## 3.`` 的正文会把 ``## 附录`` 也吞进去。
    headings: list[tuple[int, int, str | None, str]] = []  # (行号, 层级, 节号, 标题)
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is not None:
            headings.append(
                (index, len(match.group("hashes")), match.group("number"), match.group("title"))
            )
            continue
        plain = re.match(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$", line)
        if plain is not None:
            headings.append((index, len(plain.group("hashes")), None, plain.group("title")))

    sections: dict[str, Section] = {}
    for position, (index, level, number, title) in enumerate(headings):
        if number is None:
            continue
        end = len(lines)
        for next_index, next_level, _n, _t in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        sections[number] = Section(
            number=number,
            title=title,
            level=level,
            text="\n".join(lines[index:end]).strip(),
        )
    return sections


def section_of_heading_path(heading_path: str | None) -> str | None:
    """从切块器产出的章节路径里取节号：``「积分规则 > 2. 积分抵扣与使用上限 > 2.2 抵扣上限」``
    → ``2.2``。

    取**最后一段**：交付单元按标题路径前两段折叠，而引用边指向的是叶子节，所以
    「这一条命中的是第几节」只有最后一段能回答。
    """

    if not heading_path:
        return None
    tail = heading_path.split(">")[-1].strip()
    match = re.match(r"(\d+(?:\.\d+)?)", tail)
    return match.group(1) if match else None


@dataclass(frozen=True)
class CitationResult:
    """一条引用的复算结果：能不能还原、还原到哪一节、片段落在第几个字符。"""

    ok: bool
    reason: str
    document: str = ""
    section: str = ""
    section_title: str = ""
    offset: int = -1

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def resolve_citation(
    *,
    document: str,
    section: str,
    snippet: str,
    corpus_dir: Path | None = None,
    manifest: dict[str, Any] | None = None,
) -> CitationResult:
    """把「文档 + 节路径 + 片段」还原到语料原文——引用回溯率的**唯一**判定点。

    三种失败各有专码，不合并成一个 ``False``：排障时要能分清「文档名写错」「节号不存在」
    与「片段不在这一节里」——它们的修法完全不同（前者是标注错，后者可能是折叠把两节
    拼在了一起）。
    """

    root = corpus_dir or REFS_CORPUS_DIR
    layout = manifest or load_manifest(corpus_dir)
    documents: dict[str, Any] = layout["documents"]

    stem = document.strip()
    if stem.endswith(".md"):
        stem = stem[: -len(".md")]
    meta = documents.get(stem)
    if meta is None:
        match = [
            key
            for key, entry in documents.items()
            if entry.get("title") == document.strip() or entry.get("file") == document.strip()
        ]
        if not match:
            return CitationResult(ok=False, reason="unknown-document", document=stem)
        stem = match[0]
        meta = documents[stem]

    sections = split_sections((root / meta["file"]).read_text(encoding="utf-8"))
    number = section_of_heading_path(section) or section.strip()
    target = sections.get(number)
    if target is None:
        return CitationResult(
            ok=False, reason="unknown-section", document=stem, section=number
        )

    haystack = _normalize(target.text)
    needle = _normalize(snippet)
    if not needle:
        return CitationResult(
            ok=False,
            reason="empty-snippet",
            document=stem,
            section=number,
            section_title=target.title,
        )
    offset = haystack.find(needle)
    if offset < 0:
        return CitationResult(
            ok=False,
            reason="snippet-not-in-section",
            document=stem,
            section=number,
            section_title=target.title,
        )
    return CitationResult(
        ok=True,
        reason="resolved",
        document=stem,
        section=number,
        section_title=target.title,
        offset=offset,
    )


# ───────────────────────────────────────────────────────────── 运行结果的数据形状


@dataclass
class Hit:
    """一条命中：节级交付单元（ADR-0013）落在哪一节、正文开头是什么。"""

    chunk_id: str | None
    document: str
    heading_path: str | None
    content: str
    score: float | None = None
    retrieval_source: str | None = None

    @property
    def section(self) -> str | None:
        return section_of_heading_path(self.heading_path)

    @property
    def snippet(self) -> str:
        return _normalize(self.content)[:SNIPPET_CHARS]


@dataclass
class Outcome:
    """一道题的一次运行。**只装外部可观测量**：没有内部计数器，也不装模型自评。"""

    id: str
    query: str
    mechanism: str
    hits: list[Hit] = field(default_factory=list)
    stop_reason: str | None = None
    empty_reason: str | None = None
    degraded_reason: str | None = None
    latency_ms: float = 0.0
    model_calls: int = 0
    error: str | None = None

    def termination(self) -> str:
        """今天这条链路的终止形态（七种停止原因之外的等价物）。

        Research 落地之前，「为什么停」只能从三件事读出来：异常、空结果原因、降级原因。
        合成一个字符串而不是三个字段，是为了让分布表能直接计数。
        """

        if self.error:
            return f"error:{self.error}"
        if self.empty_reason:
            return f"empty:{self.empty_reason}"
        if self.degraded_reason:
            return f"degraded:{self.degraded_reason}"
        return "returned"


# ───────────────────────────────────────────────────────────────── 三种数据来源


def run_dry(*, questions: Sequence[dict[str, Any]], top_k: int) -> list[Outcome]:
    """空跑自检：**不连库、不发网络、不烧一次嵌入**。

    它返回的不是「编出来的结果」，而是一个**明说的退化**：检索器不返回任何东西，
    所以每题都是 0 命中、0 调用、0 耗时。报告因此只证明一件事——整条批处理
    （读题 → 统计 → 渲染 → 落盘）是通的，报告结构是对的。
    """

    return [
        Outcome(
            id=question["id"],
            query=question["query"],
            mechanism=question["mechanism"],
            empty_reason="dry_run_no_retriever",
        )
        for question in questions
    ]


def run_fixture(
    *, questions: Sequence[dict[str, Any]], path: Path
) -> list[Outcome]:
    """回放一份固定结果文件（C6 存下来的抓取结果）。

    文件形状故意与 ``results.json`` 的 ``per_question`` 一致，多一层都不加：这样
    「跑一次 → 存下来 → 明天离线重算」不需要转换脚本，也不会因为转换时漏一个字段
    让读数悄悄变样。
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload.get("per_question", [])}
    outcomes: list[Outcome] = []
    for question in questions:
        item = by_id.get(question["id"], {})
        outcomes.append(
            Outcome(
                id=question["id"],
                query=question["query"],
                mechanism=question["mechanism"],
                hits=[
                    Hit(
                        chunk_id=hit.get("chunk_id"),
                        document=hit.get("document", ""),
                        heading_path=hit.get("heading_path"),
                        content=hit.get("content", ""),
                        score=hit.get("score"),
                        retrieval_source=hit.get("retrieval_source"),
                    )
                    for hit in item.get("hits", [])
                ],
                stop_reason=item.get("stop_reason"),
                empty_reason=item.get("empty_reason"),
                degraded_reason=item.get("degraded_reason"),
                latency_ms=float(item.get("latency_ms") or 0.0),
                model_calls=int(item.get("model_calls") or 0),
                error=item.get("error"),
            )
        )
    return outcomes


def _hit_from_chunk(chunk: dict[str, Any], document_names: dict[str, str]) -> Hit:
    document_id = str(chunk.get("document_id") or "")
    return Hit(
        chunk_id=str(chunk.get("chunk_id")) if chunk.get("chunk_id") else None,
        document=document_names.get(document_id, document_id),
        heading_path=chunk.get("heading_path"),
        content=str(chunk.get("content") or ""),
        score=float(chunk["score"]) if isinstance(chunk.get("score"), (int, float)) else None,
        retrieval_source=str(chunk.get("retrieval_source") or "") or None,
    )


async def run_live(
    *,
    questions: Sequence[dict[str, Any]],
    kb_ids: Sequence[str],
    top_k: int,
    tenant_id: str | None = None,
) -> list[Outcome]:
    """跑今天真实的检索链路（进程内 ``rag_service.retrieve``，不经 HTTP）。

    它调的是**组合根**而不是内核：可见性校验、配额之外的那些「每次调用一次」的事
    与线上完全一致。这条路径不改 ``app/`` 任何一行——评测脚本装配应用，是既有做法
    （``scripts/eval_retrieval.py`` 同形）。

    本题集**不开启**查询改写（``rewrite=False``）：改写会把「跨文档」这一层的措辞差异
    抹平，测出来的就不是题集想测的东西了。折叠与表格行折回保持默认开启。
    """

    from app.core.config import settings  # 延迟导入：dry-run 不碰 settings
    from app.db.async_session import AsyncSessionLocal
    from app.models import Document
    from app.services.rag_service import retrieve
    from app.services.retrieval_options import RetrievalOptions
    from sqlalchemy import select

    outcomes: list[Outcome] = []
    async with AsyncSessionLocal() as db:
        document_names: dict[str, str] = {}
        rows = await db.execute(
            select(Document.id, Document.title, Document.source_filename).where(
                Document.kb_id.in_(list(kb_ids))
            )
        )
        for document_id, title, filename in rows.all():
            stem = Path(filename).stem if filename else title
            document_names[str(document_id)] = stem

        for question in questions:
            started = time.perf_counter()
            outcome = Outcome(
                id=question["id"], query=question["query"], mechanism=question["mechanism"]
            )
            try:
                response = await retrieve(
                    db,
                    tenant_id=tenant_id or settings.DEV_TENANT_ID,
                    kb_ids=list(kb_ids),
                    query=question["query"],
                    options=RetrievalOptions(
                        mode="hybrid",
                        top_k=top_k,
                        fold_to_sections=True,
                        expand_table_rows=True,
                        # 重排跟着服务端开关走：本脚本比的是「同一台机器上的今天」，
                        # 不替调用方决定要不要装重排。
                        rerank=settings.RERANK_ENABLED,
                        rewrite=False,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 单题失败是读数，不是脚本崩溃
                outcome.error = type(exc).__name__
                outcome.latency_ms = round((time.perf_counter() - started) * 1000, 2)
                outcomes.append(outcome)
                continue

            meta = response["metadata"]["retrieval"]
            outcome.empty_reason = meta.get("empty_reason")
            outcome.degraded_reason = meta.get("degraded_reason") if meta.get("degraded") else None
            outcome.hits = [
                _hit_from_chunk(chunk, document_names)
                for chunk in response.get("retrieved_chunks", [])
            ]
            outcome.latency_ms = round((time.perf_counter() - started) * 1000, 2)
            outcomes.append(outcome)
    return outcomes


# ─────────────────────────────────────────────────────────────────── 四类指标


def stop_reason_distribution(
    outcomes: Sequence[Outcome], *, research_enabled: bool, research_status: str
) -> dict[str, Any]:
    """停止原因分布：Research 的七种原因，一侧没落地时写明没落地。

    ``research_enabled=False`` 时**不编**七种原因里的任何一种：今天这条链路根本没有
    状态机，里面没有任何一种原因出现的可能。此时分布为空，由 ``status`` 字段说明
    原因（``disabled`` / ``capability-not-implemented``）。
    """

    if not research_enabled:
        return {"status": research_status, "counts": {}, "total": 0}
    counts = Counter(
        outcome.stop_reason
        for outcome in outcomes
        if outcome.stop_reason in STOP_REASONS
    )
    unknown = [
        outcome.stop_reason
        for outcome in outcomes
        if outcome.stop_reason is not None and outcome.stop_reason not in STOP_REASONS
    ]
    return {
        "status": research_status,
        "counts": dict(sorted(counts.items())),
        "total": sum(counts.values()),
        # 七种之外的值单独留档：出现了就说明状态机多了一种出口，必须回代码里看。
        "unknown": unknown,
    }


def retrieval_termination(outcomes: Sequence[Outcome]) -> dict[str, int]:
    """今天链路的终止形态分布（返回 / 空结果 / 降级 / 异常）。

    它**不是**停止原因，也从不冒充：七种原因是 Research 状态机的出口，这里是检索一次性
    返回时的几种形态。两者并列展示，读者一眼能看出「哪些原因今天不可能出现」。
    """

    counts = Counter(outcome.termination() for outcome in outcomes)
    return dict(sorted(counts.items()))


@dataclass
class TraceabilityReport:
    """引用回溯率：每一条命中都能还原到语料原文吗。

    ``rate`` 为 ``None`` 表示**没有样本**（0 条命中），而不是「回溯率 0」。这两件事在
    报告里必须分开：空跑自检不产生引用，把它印成 0.0 会被读成「引用全是错的」。
    """

    hits: int
    traceable: int
    untraceable: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        return None if self.hits == 0 else self.traceable / self.hits


def citation_traceability(
    outcomes: Sequence[Outcome],
    *,
    corpus_dir: Path | None = None,
    manifest: dict[str, Any] | None = None,
) -> TraceabilityReport:
    """逐条命中做一次 ``resolve_citation``。

    判定用的是**命中的原文**（``content``）而不是题目的期望标注：期望标注是尺子，
    回溯率问的是「这条返回到底是不是库里的原文」。把两者混在一起，就会出现
    「标注对上了、正文却是模型编的」这种最该被抓到的情况。
    """

    layout = manifest or load_manifest(corpus_dir)
    report = TraceabilityReport(hits=0, traceable=0)
    for outcome in outcomes:
        for hit in outcome.hits:
            report.hits += 1
            result = resolve_citation(
                document=hit.document,
                section=hit.heading_path or "",
                snippet=hit.snippet,
                corpus_dir=corpus_dir,
                manifest=layout,
            )
            if result.ok:
                report.traceable += 1
            else:
                report.untraceable.append(
                    {
                        "question": outcome.id,
                        "chunk_id": hit.chunk_id,
                        "document": hit.document,
                        "heading_path": hit.heading_path,
                        "reason": result.reason,
                        "snippet": hit.snippet,
                    }
                )
    return report


def coverage_by_question(
    outcomes: Sequence[Outcome], questions: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """期望命中的节有没有被取回（覆盖读数）。

    **它不进四类指标**：覆盖是「题集自己的事」，四类指标是「链路的事」。放在逐题明细里
    是因为没有它就没法解释「引用回溯率 100% 但覆盖 0」这种读数——那说明取回的全是
    别处的原文，回溯得再准也答不上问题。
    """

    expected_by_id = {question["id"]: question.get("expected", []) for question in questions}
    coverage: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        expected = expected_by_id.get(outcome.id, [])
        retrieved = {
            (hit.document, hit.section) for hit in outcome.hits if hit.section is not None
        }
        found = [
            f"{item['document']}:{item['section']}"
            for item in expected
            if (item["document"], item["section"]) in retrieved
        ]
        coverage[outcome.id] = {
            "expected": len(expected),
            "found": len(found),
            "found_keys": found,
            "missing": [
                f"{item['document']}:{item['section']}"
                for item in expected
                if f"{item['document']}:{item['section']}" not in found
            ],
        }
    return coverage


def _percentile(values: Sequence[float], point: float) -> float | None:
    """最近秩百分位。样本少的时候不做插值——插出来的数比原始读数更容易被当真。"""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(point / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def latency_stats(outcomes: Sequence[Outcome]) -> dict[str, float | None]:
    """延迟：逐题 + p50 / p95 / max。

    只统计**真的有耗时**的题（``> 0``）：0.0 在这里的含义是「没有测量」（空跑自检、
    或 fixture 里根本没记这一栏），把它算进百分位会让 p50 变成 0.0——那是「一半的题
    耗时为零」的假象，而不是任何一次真实测量。全都为 0 时整块返回 ``None``。
    """

    values = [outcome.latency_ms for outcome in outcomes if outcome.latency_ms > 0]
    if not values:
        return {"p50": None, "p95": None, "max": None, "total": None}
    return {
        "p50": round(_percentile(values, 50) or 0.0, 2),
        "p95": round(_percentile(values, 95) or 0.0, 2),
        "max": round(max(values), 2),
        "total": round(sum(values), 2),
    }


def model_call_stats(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """模型调用次数：总量 + 逐题。**只数真的发生了的**，预计值单独印在报告开头。"""

    total = sum(outcome.model_calls for outcome in outcomes)
    return {
        "total": total,
        "by_question": {outcome.id: outcome.model_calls for outcome in outcomes},
        "questions_with_calls": sum(1 for outcome in outcomes if outcome.model_calls),
    }


def predict_calls(
    *,
    source: str,
    question_count: int,
    research_enabled: bool,
    research_status: str,
) -> dict[str, Any]:
    """开跑前先算一遍：这次要花多少次调用。

    ``live`` 每题一次查询嵌入（改写关，所以向量一路只有原句）；``dry-run`` / ``fixture``
    一次都不调。Research 侧今天一律 0——不是「省了」，而是**这条路还没接**。
    """

    embedding = question_count if source == SOURCE_LIVE else 0
    research = 0
    if research_enabled and research_status == CAPABILITY_NOT_IMPLEMENTED:
        # 七种停止原因、3 次模型调用都属于还没落地的状态机；这里写 0 并在报告里
        # 标 capability-not-implemented，而不是按「未来会有 3 次」预扣。
        research = 0
    return {
        "embedding": embedding,
        "research_llm": research,
        "total": embedding + research,
        "notes": {
            "embedding": (
                f"{question_count} 题 × 1 次查询向量（--source live，改写关）"
                if embedding
                else "不连库、不发网络（--source dry-run/fixture）"
            ),
            "research_llm": (
                "未落地，capability-not-implemented，实际不会发出任何请求"
                if research_enabled
                else "未启用（research=off）"
            ),
        },
    }


# ───────────────────────────────────────────────────────────── 冻结资产（守护）


def normalized_sha256(path: Path) -> str:
    """哈希取「CRLF 折成 LF」之后的字节。

    为什么不直接哈希工作区字节：Windows 上 ``core.autocrlf=true`` 会让同一份文件在不同
    机器、不同 checkout 之后字节不同，而内容一个字都没变。用规范化后的字节，清单才能
    既守住「内容没被改」，又不被换行策略误报。
    """

    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def collect_frozen(root: Path | None = None) -> dict[str, str]:
    """``eval/corpus`` 与 ``eval/baseline`` 下每个文件的规范化 sha256。"""

    base_root = root or REPO_ROOT
    files: dict[str, str] = {}
    for relative in FROZEN_DIRS:
        directory = base_root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                files[path.relative_to(base_root).as_posix()] = normalized_sha256(path)
    return files


@dataclass
class FrozenCheck:
    """冻结资产自检结果：缺文件 / 内容变了 / 多出文件，三类分开报。"""

    checked: int
    missing: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.changed or self.extra)


def check_frozen(
    *, root: Path | None = None, manifest_path: Path | None = None
) -> FrozenCheck:
    """拿清单与工作区对账。

    三类问题分开报而不是合成一个布尔：``missing`` 是「语料被删了」、``changed`` 是
    「内容被改了」、``extra`` 是「往里加了文档」——#138 验收第 1 条最担心的正是第三类
    （往里加文档等于基线当场失效），它必须能被单独看见。
    """

    base_root = root or REPO_ROOT
    path = manifest_path or (base_root / "eval/frozen-assets.sha256.json")
    if not path.is_file():
        raise SystemExit(f"缺哈希清单 {path}；先跑 `uv run python scripts/eval_refs.py freeze --write`")
    reference: dict[str, str] = {
        key: value
        for key, value in json.loads(path.read_text(encoding="utf-8"))["files"].items()
        if any(key.startswith(f"{directory}/") for directory in FROZEN_DIRS)
    }
    actual = collect_frozen(base_root)
    result = FrozenCheck(checked=len(reference))
    for key, digest in sorted(reference.items()):
        if key not in actual:
            result.missing.append(key)
        elif actual[key] != digest:
            result.changed.append(key)
    result.extra = sorted(key for key in actual if key not in reference)
    return result


def render_frozen_manifest(root: Path | None = None) -> dict[str, Any]:
    """生成哈希清单的 JSON（不含时间戳，避免每跑一次就 diff 一大片）。"""

    base_root = root or REPO_ROOT
    return {
        "_readme": [
            "eval/corpus/ 与 eval/baseline/ 的内容指纹：票 A3（#145）一个字都没动它们，",
            "这份清单把这件事变成会红的东西，而不是一句承诺（#138 验收第 1 条）。",
            "",
            "哈希对象是把 CRLF 折成 LF 之后的字节：Windows 的 core.autocrlf 会让同一份",
            "内容在不同 checkout 上字节不同，直接哈希工作区会把换行策略误报成内容变更。",
            "",
            "**确需变更时**：先想清楚为什么（换语料 = 基线当场失效，要重跑 baseline 并归档），",
            "再跑 `uv run python scripts/eval_refs.py freeze --write` 并把 diff 一起提交——",
            "变更因此留在历史里，而不是悄悄发生。",
            "",
            "对账：`uv run python scripts/eval_refs.py check-frozen`；",
            "用例：tests/test_eval_refs.py（多出文件也算失败：往基线语料里加文档等于基线失效）。",
        ],
        "files": collect_frozen(base_root),
    }


# ─────────────────────────────────────────────────────────────── 运行与产物


@dataclass
class RunConfig:
    """一次跑的全部构成条件。**它进报告**：数字要能复现，前提是条件可被复核。"""

    source: str
    graph: str
    research: str
    allow_model_calls: bool
    kb_ids: tuple[str, ...]
    top_k: int
    fixtures: Path | None = None
    tenant_id: str | None = None


def _corpus_digest(manifest: dict[str, Any], *, corpus_dir: Path | None = None) -> str:
    """语料指纹（12 位）：9 条引用边 + 每篇文档的哈希，改一个字就变。"""

    root = corpus_dir or REFS_CORPUS_DIR
    digest = hashlib.sha256()
    for meta in sorted(manifest["documents"].values(), key=lambda item: item["file"]):
        digest.update(meta["file"].encode())
        digest.update(normalized_sha256(root / meta["file"]).encode())
    for edge in manifest["reference_inventory"]:
        digest.update(json.dumps(edge, ensure_ascii=False, sort_keys=True).encode())
    return digest.hexdigest()[:12]


def capability_status(config: RunConfig) -> dict[str, str]:
    """「开 / 关」两侧今天各自是什么状态。

    这是本票最容易被糊弄过去的一处：图谱注入位在 ``8b37f33`` 还只是签名（默认 ``None``），
    Research 接口整条不存在。两侧一律 ``capability-not-implemented``——**不是**
    ``disabled``，也不是「开了但没变化」。
    """

    return {
        # 图谱注入位：内核的 graph_injection 参数存在但恒为 None，#152 才接线。
        "graph": CAPABILITY_NOT_IMPLEMENTED,
        # Research：/rag/research 与 MCP 第三工具都还没有入口。
        "research": CAPABILITY_NOT_IMPLEMENTED,
    }


def run_batch(config: RunConfig) -> dict[str, Any]:
    """一把跑完 8 道题，返回 ``results.json`` 的完整载荷。"""

    manifest = load_manifest()
    questions_payload = load_questions()
    questions: Sequence[dict[str, Any]] = questions_payload["questions"]

    if config.source == SOURCE_DRY_RUN:
        outcomes = run_dry(questions=questions, top_k=config.top_k)
    elif config.source == SOURCE_FIXTURE:
        if config.fixtures is None:
            raise SystemExit("--source fixture 需要 --fixtures <文件>")
        outcomes = run_fixture(questions=questions, path=config.fixtures)
    else:
        outcomes = asyncio.run(
            run_live(
                questions=questions,
                kb_ids=config.kb_ids,
                top_k=config.top_k,
                tenant_id=config.tenant_id,
            )
        )

    statuses = capability_status(config)
    research_enabled = config.research == "on"
    research_status = (
        statuses["research"] if research_enabled else "disabled"
    )
    trace = citation_traceability(outcomes, manifest=manifest)
    coverage = coverage_by_question(outcomes, questions)

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": config.source,
        "switches": {"graph": config.graph, "research": config.research},
        "capabilities": statuses,
        "premise": {
            "comparable_with_original_project": False,
            "why": (
                "本仓库的语料与题集都是自己的，复现原项目那组效果数字（+4.9% / −82.5% / "
                "+3.4pp）既不成立也不可比（#138 Out of Scope）。"
            ),
        },
        "corpus": {
            "dir": "eval/corpus-refs",
            "documents": sorted(manifest["documents"]),
            "reference_edges": len(manifest["reference_inventory"]),
            "digest": _corpus_digest(manifest),
        },
        "questions": {
            "path": "eval/refs-questions.json",
            "count": len(questions),
            "by_mechanism": dict(
                sorted(Counter(question["mechanism"] for question in questions).items())
            ),
            "k": questions_payload.get("k", DEFAULT_TOP_K),
        },
        "predicted_calls": predict_calls(
            source=config.source,
            question_count=len(questions),
            research_enabled=research_enabled,
            research_status=statuses["research"],
        ),
        "metrics": {
            "stop_reason_distribution": stop_reason_distribution(
                outcomes, research_enabled=research_enabled, research_status=research_status
            ),
            "retrieval_termination": retrieval_termination(outcomes),
            "citation_traceability": {
                "hits": trace.hits,
                "traceable": trace.traceable,
                "rate": trace.rate,
                "untraceable": trace.untraceable,
            },
            "latency_ms": latency_stats(outcomes),
            "model_calls": model_call_stats(outcomes),
        },
        "coverage": coverage,
        "per_question": [
            {
                "id": outcome.id,
                "mechanism": outcome.mechanism,
                "query": outcome.query,
                "stop_reason": outcome.stop_reason,
                "termination": outcome.termination(),
                "empty_reason": outcome.empty_reason,
                "degraded_reason": outcome.degraded_reason,
                "error": outcome.error,
                "latency_ms": outcome.latency_ms,
                "model_calls": outcome.model_calls,
                "hits": [
                    {
                        "chunk_id": hit.chunk_id,
                        "document": hit.document,
                        "heading_path": hit.heading_path,
                        "section": hit.section,
                        "score": hit.score,
                        "retrieval_source": hit.retrieval_source,
                        "content": hit.content,
                    }
                    for hit in outcome.hits
                ],
            }
            for outcome in outcomes
        ],
    }


# ────────────────────────────────────────────────────────────────── 报告渲染


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} ms"


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _source_label(source: str) -> str:
    return {
        SOURCE_DRY_RUN: "空跑自检（dry-run：不连库、不发网络、不烧嵌入）",
        SOURCE_FIXTURE: "固定结果文件回放（fixture）",
        SOURCE_LIVE: "真实检索链路（进程内 retrieve，真实 PostgreSQL + 检索 Provider）",
    }[source]


def render_report(payload: dict[str, Any], config: RunConfig) -> str:
    """人读的报告。四类指标各一节，**未落地的能力写在指标旁边**而不是脚注里。"""

    metrics = payload["metrics"]
    statuses = payload["capabilities"]
    distribution = metrics["stop_reason_distribution"]
    trace = metrics["citation_traceability"]
    latency = metrics["latency_ms"]
    calls = metrics["model_calls"]
    predicted = payload["predicted_calls"]

    lines: list[str] = []
    add = lines.append
    add(f"# 引用语料跑批报告 · {payload['run_id']}")
    add("")
    add(f"- 生成时间：{payload['generated_at']}")
    add(f"- 数据来源：{_source_label(payload['source'])}")
    add(f"- 开关：graph={config.graph} / research={config.research}")
    add(
        f"- 语料：`eval/corpus-refs/`（{len(payload['corpus']['documents'])} 篇，"
        f"{payload['corpus']['reference_edges']} 条引用边，指纹 `{payload['corpus']['digest']}`）"
    )
    add(
        f"- 题库：`eval/refs-questions.json`（{payload['questions']['count']} 道，"
        f"四类机制各 2 道；k={payload['questions']['k']}）"
    )
    add(
        "- 模型调用：本次实际 "
        f"**{calls['total']} 次**（开跑前预计 {predicted['total']} 次："
        f"嵌入 {predicted['embedding']} + Research {predicted['research_llm']}）"
    )
    add("")
    add(
        "> **与原项目的数字不可比**：语料与题集都是本仓库自己的。原项目那组效果数字"
        "（+4.9% / −82.5% / +3.4pp）在 #138 里已明确不复现，也不作为承诺。"
    )
    if payload["source"] == SOURCE_DRY_RUN:
        add(">")
        add(
            "> **本次是空跑自检**：检索器故意不返回任何结果，所以下面所有「0」都表示"
            "「没有测量」，不是「测量结果为零」。它只证明批处理与报告结构是通的。"
        )
    add("")

    add("## 这次跑的是什么")
    add("")
    add("| 项 | 值 | 复现命令 |")
    add("| --- | --- | --- |")
    add(
        "| 数据来源 | "
        f"{payload['source']} | `uv run python scripts/eval_refs.py --source {payload['source']}` |"
    )
    add(
        "| 知识库 | "
        f"{', '.join(config.kb_ids)} | `uv run python scripts/eval_refs.py --source live "
        f"--allow-model-calls --kb-ids {config.kb_ids[0]}` |"
    )
    add(
        "| 返回条数 | "
        f"top_k={config.top_k} | `uv run python scripts/eval_refs.py --top-k {config.top_k}` |"
    )
    add(
        "| 预计模型调用 | "
        f"嵌入 {predicted['embedding']} + Research {predicted['research_llm']}"
        f" = {predicted['total']} | 见 `results.json` 的 `predicted_calls` |"
    )
    add("")

    add("## 一、停止原因分布")
    add("")
    add("Research 的七种固定停止原因（#138 §C）：" + "、".join(f"`{name}`" for name in STOP_REASONS))
    add("")
    if distribution["status"] == CAPABILITY_NOT_IMPLEMENTED:
        add(
            f"**{CAPABILITY_NOT_IMPLEMENTED}**：`--research {config.research}` 要求的那一侧还没有"
            "入口（`/rag/research` 与 MCP 第三工具都不存在），所以七种原因一种都没有出现——"
            "这不是「都试过了，结果是这些」。"
        )
    elif distribution["status"] == "disabled":
        add(
            "**未启用**（research=off）：本次不跑 Research，七种原因不适用。"
            "要读这一侧，等 C3 落地后加 `--research on`。"
        )
    else:
        add("| 停止原因 | 次数 | 占比 |")
        add("| --- | --- | --- |")
        for name, count in distribution["counts"].items():
            share = count / distribution["total"] if distribution["total"] else 0.0
            add(f"| `{name}` | {count} | {share:.2%} |")
        if not distribution["counts"]:
            add("| （无） | 0 | n/a |")
    add("")
    add("### 今天链路的终止形态（等价物，**不是**停止原因）")
    add("")
    add(
        "Research 落地之前，「为什么停」只能从这三件事读出来。它与上表并列，"
        "不冒充七种原因里的任何一种。"
    )
    add("")
    add("| 终止形态 | 题数 |")
    add("| --- | --- |")
    for name, count in metrics["retrieval_termination"].items():
        add(f"| `{name}` | {count} |")
    add("")

    add("## 二、引用回溯率")
    add("")
    add(
        "判定：每一条命中都拿「文档 + 节路径 + 原文片段」回语料原文里找一遍，找得到才算可回溯。"
        "同一段逻辑既服务这里，也服务 `verify-citation`——两处口径不可能分叉。"
    )
    add("")
    add("| 指标 | 值 |")
    add("| --- | --- |")
    add(f"| 命中的交付单元数 | {trace['hits']} |")
    add(f"| 可回溯 | {trace['traceable']} |")
    add(f"| 回溯率 | {_fmt_rate(trace['rate'])}（n/a = 没有样本，不是「回溯率 0」） |")
    add("")
    if trace["untraceable"]:
        add("不可回溯的命中（说明它没能在语料里找到对应片段：查文档名、节号与片段是否被改写）：")
        add("")
        add("| 题号 | 文档 | 节路径 | 失败原因 | 片段 |")
        add("| --- | --- | --- | --- | --- |")
        for item in trace["untraceable"]:
            add(
                f"| {item['question']} | {item['document']} | {item['heading_path'] or '—'} "
                f"| `{item['reason']}` | {item['snippet']} |"
            )
        add("")
    add("**手工复算一条引用**（把 `<文档> <节号> <片段>` 换成报告里那一条）：")
    add("")
    add("```bash")
    add(
        "uv run python scripts/eval_refs.py verify-citation --document refund_policy "
        '--section 3 --snippet "银行卡一般 3 个工作日"'
    )
    add("```")
    add("")

    add("## 三、延迟")
    add("")
    if latency["p50"] is None:
        add(
            "**n/a**：本次没有真实耗时样本（空跑自检不产生耗时，fixture 里也没有记这一栏）。"
            "印一个 `0.00 ms` 会被当成本次测量的读数。"
        )
    else:
        add("只统计有真实耗时的题（`0.0` 一律算「未测量」，不进百分位）。")
        add("")
        add("| 指标 | 值 |")
        add("| --- | --- |")
        add(f"| p50 | {_fmt_ms(latency['p50'])} |")
        add(f"| p95 | {_fmt_ms(latency['p95'])} |")
        add(f"| max | {_fmt_ms(latency['max'])} |")
        add(f"| 合计 | {_fmt_ms(latency['total'])} |")
    add("")

    add("## 四、模型调用次数")
    add("")
    add("| 项 | 值 |")
    add("| --- | --- |")
    add(f"| 实际调用 | {calls['total']} |")
    add(f"| 有调用的题数 | {calls['questions_with_calls']} |")
    add(f"| 开跑前预计 | {predicted['total']}（嵌入 {predicted['embedding']}、Research {predicted['research_llm']}） |")
    add("")
    add(f"- 嵌入：{predicted['notes']['embedding']}")
    add(f"- Research：{predicted['notes']['research_llm']}")
    add("")

    add("## 能力开关状态")
    add("")
    add("| 开关 | 本次取值 | 状态 | 说明 |")
    add("| --- | --- | --- | --- |")
    add(
        f"| graph | {config.graph} | `{statuses['graph']}` | 内核的图谱注入位在 `8b37f33` "
        "只有签名（默认 `None`），注入随 #152 接线；此刻 on 与 off 的读数**必然相同**，"
        "不能据此说「图谱无效」 |"
    )
    add(
        f"| research | {config.research} | `{statuses['research']}` | `/rag/research` 与 MCP "
        "`rag_research` 都还没有入口，七种停止原因、硬上限、引用块校验一条都不存在 |"
    )
    add("")
    if config.graph == "on" or config.research == "on":
        add(
            "> **对照有效性**：本次有一侧被打开，但那一侧是 "
            f"`{CAPABILITY_NOT_IMPLEMENTED}`，所以 on / off 两组读数会是同一组数。"
            "这是「还没接」而不是「接了没用」——真实对照等 A2 / C3 落地后由 C6 出。"
        )
        add("")

    add("## 逐题明细")
    add("")
    add(
        "逐题明细见 `results.json` 的 `per_question`；下表是摘要。"
        "覆盖 = 期望命中的节取回了几节（它不进四类指标：覆盖率是题集的事，"
        "四类指标是链路的事）。"
    )
    add("")
    add("| 题号 | 机制 | 命中 | 覆盖 | 终止形态 | 延迟 | 模型调用 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for item in payload["per_question"]:
        cover = payload["coverage"][item["id"]]
        # 空跑自检 / fixture 未记这一栏时 latency 是 0：印「未测量」，不印 0.00 ms。
        latency_cell = "未测量" if item["latency_ms"] <= 0 else _fmt_ms(item["latency_ms"])
        add(
            f"| {item['id']} | {item['mechanism']} | {len(item['hits'])} "
            f"| {cover['found']}/{cover['expected']} | `{item['termination']}` "
            f"| {latency_cell} | {item['model_calls']} |"
        )
    add("")

    add("## 复算与自检")
    add("")
    add("```bash")
    add("# 默认（空跑自检）：不连库、不发网络、零模型调用，产出结构正确的报告")
    add("uv run python scripts/eval_refs.py")
    add("")
    add("# 引用回溯率机械复算：给一条引用，还原到语料原文（退出码 0 = 能还原）")
    add('uv run python scripts/eval_refs.py verify-citation --document points_policy --section 2.2 --snippet "银卡及以上时，200 元的固定上限提高到 500 元"')
    add("")
    add("# 冻结资产自检：eval/corpus 与 eval/baseline 是否被改过（多出文件也算失败）")
    add("uv run python scripts/eval_refs.py check-frozen")
    add("```")
    add("")
    return "\n".join(lines)


def write_run(payload: dict[str, Any], config: RunConfig, *, out_dir: Path) -> Path:
    """落盘：``results.json`` + ``report.md``。目录按 run_id 分，重跑不覆盖上一次。"""

    target = out_dir / payload["run_id"]
    target.mkdir(parents=True, exist_ok=True)
    (target / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "report.md").write_text(render_report(payload, config), encoding="utf-8")
    return target


# ────────────────────────────────────────────────────────────────── 命令行


def _print_predicted(predicted: dict[str, Any]) -> None:
    """开跑前把预计调用次数印出来——花不花钱要在按下回车之前就知道。"""

    print("预计模型调用次数：")
    print(f"  嵌入      : {predicted['embedding']} 次 — {predicted['notes']['embedding']}")
    print(
        f"  Research  : {predicted['research_llm']} 次 — {predicted['notes']['research_llm']}"
    )
    print(f"  合计      : {predicted['total']} 次")


def _command_run(args: argparse.Namespace) -> int:
    config = RunConfig(
        source=args.source,
        graph=args.graph,
        research=args.research,
        allow_model_calls=args.allow_model_calls,
        kb_ids=tuple(args.kb_ids),
        top_k=args.top_k,
        fixtures=Path(args.fixtures) if args.fixtures else None,
        tenant_id=args.tenant_id,
    )
    if config.source == SOURCE_LIVE and not config.allow_model_calls:
        # 真实链路每题要烧一次查询嵌入。没有显式参数就不发请求——默认值站在「不花钱」这边。
        print(
            "拒绝执行：--source live 会真的调用嵌入 Provider（每题 1 次）。\n"
            "确需花这笔钱时显式加 --allow-model-calls；想先看结构就跑默认的空跑自检。",
            file=sys.stderr,
        )
        return 2

    questions = load_questions()["questions"]
    _print_predicted(
        predict_calls(
            source=config.source,
            question_count=len(questions),
            research_enabled=config.research == "on",
            research_status=capability_status(config)["research"],
        )
    )

    payload = run_batch(config)
    target = write_run(payload, config, out_dir=Path(args.out))
    calls = payload["metrics"]["model_calls"]["total"]
    trace = payload["metrics"]["citation_traceability"]
    print(
        f"完成：{payload['questions']['count']} 题，模型调用 {calls} 次，"
        f"引用回溯 {trace['traceable']}/{trace['hits']}（{_fmt_rate(trace['rate'])}）"
    )
    print(f"产物：{target / 'report.md'}（结构同目录 results.json）")
    return 0


def _command_verify_citation(args: argparse.Namespace) -> int:
    result = resolve_citation(
        document=args.document, section=args.section, snippet=args.snippet
    )
    payload = result.as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if result.ok:
        print(
            f"可还原：{result.document} 第 {result.section} 节「{result.section_title}」，"
            f"片段起点 {result.offset}"
        )
        return 0
    print(f"不可还原：{result.reason}", file=sys.stderr)
    return 1


def _command_check_frozen(args: argparse.Namespace) -> int:
    result = check_frozen()
    print(f"检查 {result.checked} 个文件（{'、'.join(FROZEN_DIRS)}）")
    if result.ok:
        print("通过：内容与哈希清单逐条一致")
        return 0
    for name in result.missing:
        print(f"缺失：{name}", file=sys.stderr)
    for name in result.changed:
        print(f"内容已变：{name}", file=sys.stderr)
    for name in result.extra:
        print(f"多出文件：{name}", file=sys.stderr)
    return 1


def _command_freeze(args: argparse.Namespace) -> int:
    manifest = render_frozen_manifest()
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if not args.write:
        print(text)
        print(f"（未写入；加 --write 才会写 {FROZEN_MANIFEST.relative_to(REPO_ROOT)}）")
        return 0
    FROZEN_MANIFEST.write_text(text, encoding="utf-8")
    print(f"已写入 {FROZEN_MANIFEST.relative_to(REPO_ROOT)}（{len(manifest['files'])} 个文件）")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reference-corpus batch runner (issue #145): 8 targeted questions over "
            "eval/corpus-refs/, producing stop-reason distribution, citation traceability, "
            "latency and model-call counts. Defaults to a dry run: no database, no network."
        )
    )
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="跑 8 道题并出报告（默认子命令）")
    run.add_argument(
        "--source",
        choices=SOURCE_ORDER,
        default=SOURCE_DRY_RUN,
        help="dry-run（默认，空跑自检）/ fixture（回放固定结果）/ live（真实链路，要库与凭据）",
    )
    run.add_argument("--graph", choices=("off", "on"), default="off", help="图谱注入侧（#152）")
    run.add_argument(
        "--research", choices=("off", "on"), default="off", help="Research 侧（#148）"
    )
    run.add_argument(
        "--allow-model-calls",
        action="store_true",
        help="允许真实模型调用。--source live 必须显式加它，否则拒绝执行",
    )
    run.add_argument("--fixtures", default=None, help="--source fixture 的结果文件")
    run.add_argument(
        "--kb-ids", nargs="+", default=["kb-refs"], help="真实链路要查的知识库（默认 kb-refs）"
    )
    run.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="每题返回条数")
    run.add_argument("--tenant-id", default=None, help="租户（默认 settings.DEV_TENANT_ID）")
    run.add_argument(
        "--out", default=str(DEFAULT_RUNS_DIR), help="产物根目录（默认 eval/refs-runs）"
    )
    run.set_defaults(func=_command_run)

    verify = subparsers.add_parser("verify-citation", help="把一条引用还原到语料原文")
    verify.add_argument("--document", required=True, help="文档名（stem 或文件名）")
    verify.add_argument(
        "--section", required=True, help="节号（3 / 2.2）或整条标题路径（末段取节号）"
    )
    verify.add_argument("--snippet", required=True, help="要还原的原文片段")
    verify.set_defaults(func=_command_verify_citation)

    frozen = subparsers.add_parser("check-frozen", help="检查 eval/corpus 与 eval/baseline")
    frozen.set_defaults(func=_command_check_frozen)

    freeze = subparsers.add_parser("freeze", help="生成/查看冻结资产哈希清单")
    freeze.add_argument("--write", action="store_true", help="写入 eval/frozen-assets.sha256.json")
    freeze.set_defaults(func=_command_freeze)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    # 无子命令 = ``run``（票要求「一条命令跑」）：``eval_refs.py --graph on`` 要能直接跑。
    # 只有显式的子命令名与顶层 --help 走原样解析，其余一律补上 ``run``——
    # 补在前面而不是补在后面，参数顺序才与 help 里写的一致。
    if raw and raw[0] not in (*COMMANDS, "-h", "--help"):
        raw = ["run", *raw]
    elif not raw:
        raw = ["run"]
    args = parser.parse_args(raw)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
