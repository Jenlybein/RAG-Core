"""引用语料跑批骨架：8 道定向题 → 四类机械可核验的读数（票 A3 / #145）。

**它是什么**：一条命令，把 ``eval/refs-questions.json`` 的 8 道题跑一遍**今天**的检索链路，
产出四类指标：停止原因分布、引用回溯率、延迟、模型调用次数。它**不是**效果验收——
对照结论由 C6（#153）在 A2 / C3 落地之后出（#138 §Further Notes），本脚本只搭台子。

**「能力在不在」是探测出来的，「这次跑没跑」是另一件事**（#156）：

* 两侧的状态一律**探测代码本身**——图谱看注入服务与组合根那条缝装没装
  （``app/services/graph_injection.py`` + ``rag_service.retrieve`` 的 ``graph_factory``），
  Research 看服务与路由在不在（``research_gate.gated_research`` + ``/rag/research``）。
  **不读 ``--graph`` / ``--research`` 开关**：开关是「这次想不想跑」，探测是「这台机器上有没有」，
  把两者混成一个状态，就会出现「开关关了所以报未实现」这种自相矛盾的读数。
  两个能力都就位之后，这里输出 ``available``，不再输出 ``capability-not-implemented``。
* 光有 ``available`` 还不够诚实：**接线**是 #153（C6）这一步做的——``--graph on`` 真的把
  组合根的 ``graph_factory`` 递下去，``--research on`` 真的走 ``gated_research``。于是报告里
  那一栏 ``exercised`` 现在读的是「这次跑有没有真的驱动那一侧」：
  ``live`` + 开关开 + 能力就位（图谱还要求**重排也开着**，因为图谱注入跟着重排走）才算 ``True``。
  空跑自检、fixture 回放、或者重排关着时，它一律 ``False``，报告照旧明写「没采集」——
  让 on / off 两组相同的数字冒充对照，是这份报告最不该犯的错。

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
import inspect
import json
import math
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
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

#: 「能力已经在代码里」的唯一写法（#156 起两侧都可能出现它）。
#: 它与 ``capability-not-implemented`` 是**探测结果**，与 ``--graph`` / ``--research``
#: 那些开关无关：开关说「这次想不想跑」，探测说「这台机器上有没有」。
CAPABILITY_AVAILABLE = "available"

#: 重排开关的三个取值。``auto`` = 跟服务端配置走（缺省，行为与接线之前逐字节相同）。
#:
#: 它不是「调参」而是**可复现性**：图谱注入跟着重排走（ADR-0027 开关归属），重排关着时
#: ``--graph on`` 这一侧压根不会被装配。把重排写成参数，报告里那组 on / off 才说得清
#: 「同一台机器、同一个重排状态、只差图谱」。
RERANK_AUTO = "auto"
RERANK_ON = "on"
RERANK_OFF = "off"
RERANK_OPTIONS = (RERANK_AUTO, RERANK_ON, RERANK_OFF)

#: 三类数据来源。``dry-run`` 是缺省——**默认不花钱**这条纪律落在默认值上，
#: 而不是落在某个人记得加参数上。
SOURCE_DRY_RUN = "dry-run"
SOURCE_FIXTURE = "fixture"
SOURCE_LIVE = "live"
SOURCE_ORDER = (SOURCE_DRY_RUN, SOURCE_FIXTURE, SOURCE_LIVE)

#: 数据面 Key 的环境变量名（与 ``scripts/eval_runner.py`` 同一套：命令行参数优先）。
ENV_API_KEY = "EVAL_API_KEY"

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
        return CitationResult(ok=False, reason="unknown-section", document=stem, section=number)

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
    #: 图谱注入了几条候选、从几个种子扩出去（``metadata.retrieval.graph``）。
    #: ``None`` = 这一栏在响应里不存在（关掉图谱 / Research 侧拿不到）。
    graph_injected: int | None = None
    graph_seeds: int | None = None
    #: Research 侧的三态完整度与引用块（``evidence_state`` / ``citations``）。
    evidence_state: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)

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

    # ── 引用块（Research 侧可机械核验的那几样） ──

    @property
    def citation_blocks(self) -> int:
        """引用块数：Finalizer 交回的每一块都算一块（含「材料不足」那些）。"""

        return len(self.citations)

    @property
    def citations_with_evidence(self) -> int:
        """带证据下标的块数。没有证据的块必须是 ``insufficient``，否则就是伪造引用。"""

        return sum(1 for block in self.citations if block.get("evidence_ids"))

    @property
    def dangling_evidence_ids(self) -> list[str]:
        """指向**材料集合之外**的 evidence 下标：那是「引用可回溯」的反面。

        合法下标就是材料顺序（E1…En，n = 交付单元数）：编号由后端按材料顺序分配，
        所以同一条判据在这里也成立，不必再回库查一次。
        """

        valid = {f"E{index}" for index in range(1, len(self.hits) + 1)}
        dangling: list[str] = []
        for block in self.citations:
            for evidence_id in block.get("evidence_ids") or []:
                if evidence_id not in valid and evidence_id not in dangling:
                    dangling.append(str(evidence_id))
        return dangling


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


def run_fixture(*, questions: Sequence[dict[str, Any]], path: Path) -> list[Outcome]:
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


@dataclass(frozen=True)
class LiveSeams:
    """真实链路的两个注入缝：一次检索、一次 Research（与生产签名同形）。

    做成数据类而不是两个模块级函数，是为了让「这一题到底走了哪条路」在用例里可替换：
    两个替身把参数记下来，于是「``--graph on`` 有没有真的递工厂」「``--research on``
    有没有真的调门禁」都能在**不连库、不发网络**的前提下断言。
    """

    retrieve: Callable[..., Awaitable[dict[str, Any]]]
    research: Callable[..., Awaitable[Any]]


def live_seams() -> LiveSeams:
    """生产实现（惰性导入：dry-run 不 import 应用）。"""

    from app.services.rag_service import retrieve
    from app.services.research_gate import gated_research

    return LiveSeams(retrieve=retrieve, research=gated_research)


def graph_factory_for(switch: str) -> Any:
    """``--graph on`` → 生产读模型 ``DbSectionGraph``；``off`` → 显式 ``None``（关闭这一步）。

    ``None`` 不是「空壳工厂」而是「没有这一步」：组合根因此连关系查询都不发，
    响应里也不会出现 ``metadata.retrieval.graph`` 那一栏（黄金哈希守的就是这个）。
    """

    if switch != "on":
        return None
    from app.services.graph_injection import DbSectionGraph

    return DbSectionGraph


class _BearerRequest:
    """``resolve_identity`` 要的最小请求形状：只读 ``headers``。

    走它而不是自己拼一个 ``KeyContext``，是为了让**三层授权**（白名单 / 配置 / 套餐）
    与线上完全同一条路径——评测自己造一个上下文，就等于把要被验证的门禁绕过去了。
    """

    def __init__(self, api_key: str) -> None:
        self.headers = {"authorization": f"Bearer {api_key}"}


def eval_key_context(*, tenant_id: str | None = None) -> Any:
    """没有 Key 时的**开发身份**：只够跑检索那一路（Research 门禁要读配置与套餐）。

    这是既有行为（``settings.DEV_TENANT_ID`` + 内置最高档快照），保留它是因为「只比检索」
    这件事不该逼着评测机器去配一把 Key；而 Research 侧走不通时它会明确报错，不是静默降级。
    """

    from app.core.auth import KeyContext
    from app.core.config import settings

    return KeyContext(tenant_id=tenant_id or settings.DEV_TENANT_ID)


async def _resolve_key(
    db: Any, *, api_key: str | None, tenant_id: str | None, research: str
) -> Any:
    """这一轮用哪个身份：给了 Key 就走生产鉴权链，否则用开发身份（仅够检索）。"""

    if api_key:
        from app.core.auth import resolve_identity
        from app.core.exceptions import AppError

        try:
            return await resolve_identity(_BearerRequest(api_key), db)
        except AppError as exc:  # 业务错误：把话说明白，别吐 traceback
            raise SystemExit(f"鉴权失败（{exc.code}）：{exc.msg}") from exc
    if research == "on":
        raise SystemExit(
            "--research on 需要 --api-key（或环境变量 EVAL_API_KEY）：Research 的门禁要读"
            "密钥配置与套餐，凭空造一个上下文等于跳过三层授权。"
        )
    return eval_key_context(tenant_id=tenant_id)


def _apply_rerank_switch(switch: str) -> bool:
    """把 ``--rerank`` 钉到 ``settings`` 上，返回**实际生效**的值（与既有评测脚本同形）。"""

    from app.core.config import settings

    if switch == RERANK_ON:
        settings.RERANK_ENABLED = True
    elif switch == RERANK_OFF:
        settings.RERANK_ENABLED = False
    return bool(settings.RERANK_ENABLED)


def _effective_rerank(config: RunConfig) -> bool:
    """这一轮**实际**开没开重排（报告与预计调用都要用它，不能只看参数）。

    显式 ``--rerank on/off`` 优先：那是对这台机器这一次的**决定**，不该被 ``.env`` 里的
    偶然值盖掉；``auto`` 才回落到服务端配置。空跑 / 回放不连库，``auto`` 就是关。
    """

    if config.rerank == RERANK_ON:
        return True
    if config.rerank == RERANK_OFF:
        return False
    if config.source != SOURCE_LIVE:
        return False
    from app.core.config import settings

    return bool(settings.RERANK_ENABLED)


async def _document_names(db: Any, kb_ids: Sequence[str]) -> dict[str, str]:
    """``document_id → 文件名主干``：引用复算按文件名对语料（标题只是兜底）。"""

    from sqlalchemy import select

    from app.models import Document

    rows = await db.execute(
        select(Document.id, Document.title, Document.source_filename).where(
            Document.kb_id.in_(list(kb_ids))
        )
    )
    names: dict[str, str] = {}
    for document_id, title, filename in rows.all():
        names[str(document_id)] = Path(filename).stem if filename else title
    return names


def _apply_retrieval(
    outcome: Outcome, response: Mapping[str, Any], document_names: Mapping[str, str]
) -> None:
    """把一次 ``retrieve`` 的响应翻译成读数（四个字段 + 图谱注入 + 模型调用）。"""

    meta = ((response.get("metadata") or {}).get("retrieval")) or {}
    outcome.empty_reason = meta.get("empty_reason")
    outcome.degraded_reason = meta.get("degraded_reason") if meta.get("degraded") else None
    graph = meta.get("graph")
    if isinstance(graph, Mapping):
        outcome.graph_injected = int(graph.get("injected") or 0)
        outcome.graph_seeds = int(graph.get("seeds") or 0)
    outcome.hits = [
        _hit_from_chunk(chunk, dict(document_names))
        for chunk in response.get("retrieved_chunks") or []
    ]
    # 一次检索花掉几次模型调用：查询嵌入一次；**重排真的发生了**就再加一次。
    # 判据是 ``metadata.retrieval.rerank.applied``（这一栏恒存在，取的是「apply 过没有」）：
    # 把整个字典的真假当判据会给每一次检索都多加一次（#153 的守分抽样上就这么错了一次）。
    job = meta.get("rerank") or {}
    outcome.model_calls = 1 + (1 if job.get("applied") else 0)


def _as_mapping(data: Any) -> Mapping[str, Any]:
    """响应 → 只读映射。``gated_research`` 交回的是 pydantic 模型，fixture 回放的是 dict。

    两种形状都得收：真实链路只会给模型，而报告与用例只认映射。``mode="json"`` 让枚举
    （``StopReason``）落成字符串——报告里印 ``evidence_complete``，不是 ``StopReason.X``。
    """

    if isinstance(data, Mapping):
        return data
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError(f"无法读取响应：{type(data).__name__}")


def _apply_research(
    outcome: Outcome,
    data: Mapping[str, Any],
    document_names: Mapping[str, str],
    *,
    rerank_enabled: bool,
) -> None:
    """把一次 ``gated_research`` 的响应翻译成读数（七种原因 + 完整度 + 引用块 + 调用数）。

    材料**按交付单元进 ``hits``**：于是引用回溯率与覆盖读数对 Research 侧同样成立，
    而不是只有检索侧才算得出「这段材料是不是库里的原文」。
    """

    data = _as_mapping(data)
    meta = data.get("metadata") or {}
    outcome.stop_reason = meta.get("stop_reason")
    outcome.evidence_state = meta.get("evidence_state")
    outcome.degraded_reason = (meta.get("sub_search_degraded_reasons") or [None])[0] or meta.get(
        "strong_model_fallback"
    )
    outcome.hits = [
        _hit_from_chunk(row, dict(document_names)) for row in data.get("materials") or []
    ]
    outcome.citations = [dict(block) for block in data.get("citations") or []]
    # 模型调用 = 状态机自己记的 LLM 次数 + 每条子检索的查询嵌入（重排开着再加一次/条）。
    # 子检索条数是**响应里的事实**（``rounds.searches``），不是按上限估的。
    searches = int((data.get("rounds") or {}).get("searches") or 0)
    per_search = 1 + (1 if rerank_enabled else 0)
    outcome.model_calls = int(meta.get("model_calls") or 0) + searches * per_search


async def evaluate_live_question(
    question: Mapping[str, Any],
    *,
    seams: LiveSeams,
    db: Any,
    key: Any,
    kb_ids: Sequence[str],
    top_k: int,
    graph: str,
    research: str,
    document_names: Mapping[str, str],
    rerank_enabled: bool = False,
) -> Outcome:
    """跑**一道题**：``research=on`` 走门禁化的 Research，否则走一次检索内核。

    两个开关只在这一个函数里分叉，且两侧的读数都从**响应**里读（不是按配置推断）。
    异常收成 ``error:<类型>``：单题失败是读数，不是脚本崩溃。
    """

    started = time.perf_counter()
    outcome = Outcome(
        id=str(question["id"]),
        query=str(question["query"]),
        mechanism=str(question["mechanism"]),
    )
    try:
        if research == "on":
            data = await seams.research(
                db,
                key=key,
                kb_ids=list(kb_ids),
                query=outcome.query,
            )
            _apply_research(outcome, data, document_names, rerank_enabled=rerank_enabled)
        else:
            from app.services.retrieval_options import RetrievalOptions

            response = await seams.retrieve(
                db,
                tenant_id=key.tenant_id,
                kb_ids=list(kb_ids),
                query=outcome.query,
                options=RetrievalOptions(
                    mode="hybrid",
                    top_k=top_k,
                    fold_to_sections=True,
                    expand_table_rows=True,
                    # 重排是「同一台机器上的今天」的一部分，由 --rerank 钉死；
                    # 图谱注入跟着它走（ADR-0027 开关归属）。
                    rerank=rerank_enabled,
                    rewrite=False,
                    # 切片编排**关着**：它一次就多一次模型调用，而这三组对照（版本 / 图谱 /
                    # Research）没有一组在测它；开着还会给响应多加一个键，把「只差一个
                    # 开关」这件事搅浑。这一栏是 `RetrievalOptions` 的必填项（#146），
                    # 不写会当场 TypeError——这正是本票接线时抓到的第一处（旧采集路径
                    # 自 #146 起就是坏的：8 题会全落成 error:TypeError）。
                    orchestration=False,
                ),
                # 接线就是这一行：on → 生产读模型，off → None（这一步不存在）。
                graph_factory=graph_factory_for(graph),
            )
            _apply_retrieval(outcome, response, document_names)
    except Exception as exc:  # noqa: BLE001 - 单题失败是读数，不是脚本崩溃
        outcome.error = type(exc).__name__
    outcome.latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return outcome


async def _bind_model_chain() -> dict[str, Any]:
    """把模型接入接到**生产那条链**（``model_configs`` 的角色指针），返回用的是哪一条。

    与 ``scripts/eval_runner.bind_llm_role_resolver`` 是同一件事、同一组入口
    （``app.providers.llm.set_role_resolver`` + ``model_config_service.role_endpoint``）。
    这里不 import 那个函数，是因为它顺手把整个评测运行器拖进来；但口径必须一致，所以
    注释里点名它——两个入口接的不是同一条链，读数就不是同一个东西。

    为什么非要接：``.env`` 里的单供应商 key 与库里配置的那一条不是同一个账号。走错那条时
    Research 的规划/判断/生成三处各失败一次、``stop_reason`` 全落 ``degraded``——那是
    「评测没接生产链路」，不是「Research 不行」（#115 在评测侧踩过同一脚）。
    """

    from app.core.config import settings
    from app.providers.llm import set_role_resolver

    from app.services import model_config_service

    set_role_resolver(model_config_service.role_endpoint)
    endpoint = await model_config_service.role_endpoint("fast")
    if endpoint is None:
        # 回落 ``.env`` 是既定行为，不是错误；但要如实记下来，别让读数以为是库里那条。
        return {"llm_endpoint_source": "env", "llm_model": str(settings.LLM_MODEL)}
    return {
        "llm_endpoint_source": "model_configs",
        "llm_model": str(endpoint.model),
        "llm_config_id": str(endpoint.config_id),
    }


async def run_live(
    *,
    questions: Sequence[dict[str, Any]],
    kb_ids: Sequence[str],
    top_k: int,
    tenant_id: str | None = None,
    graph: str = "off",
    research: str = "off",
    rerank: str = RERANK_AUTO,
    api_key: str | None = None,
    seams: LiveSeams | None = None,
) -> list[Outcome]:
    """跑真实链路（进程内 ``rag_service.retrieve`` / ``research_gate.gated_research``）。

    它调的是**组合根与门禁**而不是内核：可见性校验、三层授权、配额之外的那些「每次调用
    一次」的事与线上完全一致。这条路径不改 ``app/`` 任何一行——评测脚本装配应用，是既有
    做法（``scripts/eval_retrieval.py`` 同形）。

    本题集**不开启**查询改写（``rewrite=False``）：改写会把「跨文档」这一层的措辞差异
    抹平，测出来的就不是题集想测的东西了。折叠与表格行折回保持默认开启。
    """

    from app.db.async_session import AsyncSessionLocal

    rerank_enabled = _apply_rerank_switch(rerank)
    resolved = seams or live_seams()
    outcomes: list[Outcome] = []
    async with AsyncSessionLocal() as db:
        key = await _resolve_key(db, api_key=api_key, tenant_id=tenant_id, research=research)
        document_names = await _document_names(db, kb_ids)
        for question in questions:
            outcomes.append(
                await evaluate_live_question(
                    question,
                    seams=resolved,
                    db=db,
                    key=key,
                    kb_ids=kb_ids,
                    top_k=top_k,
                    graph=graph,
                    research=research,
                    document_names=document_names,
                    rerank_enabled=rerank_enabled,
                )
            )
    return outcomes


async def active_index_versions(
    kb_ids: Sequence[str], *, tenant_id: str | None = None
) -> dict[str, str]:
    """这一轮查的是哪个索引版本（``kb_id → 生效版本``）：报告里「哪一次构建」的答案。

    读的是知识库行上的那一列——与检索侧 ``resolve_index_versions`` 同一个来源。
    读不到（库不存在 / 无权）就返回空映射：它是旁证，不该让整份报告崩掉。
    """

    from sqlalchemy import select

    from app.core.config import settings
    from app.db.async_session import AsyncSessionLocal
    from app.models.index_version import effective_index_version
    from app.models.knowledge_base import KnowledgeBase

    scope_tenant = tenant_id or settings.DEV_TENANT_ID
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.tenant_id == scope_tenant,
                        KnowledgeBase.id.in_(list(kb_ids)),
                    )
                )
            ).scalars()
            return {str(kb.id): effective_index_version(kb) or "" for kb in rows.all()}
    except Exception:  # noqa: BLE001 - 旁证读不到不该让报告失败
        return {}


# ─────────────────────────────────────────────────────────────────── 四类指标


def stop_reason_distribution(
    outcomes: Sequence[Outcome],
    *,
    research_enabled: bool,
    research_status: str,
    exercised: bool,
) -> dict[str, Any]:
    """停止原因分布：Research 的七种原因，一侧没采集 / 没落地时写明是哪一种。

    三种「空表」必须分开（#156）：

    * ``research_enabled=False``：这次不要跑（``status=disabled``）；
    * ``status=capability-not-implemented``：代码里没有这条路；
    * ``status=available`` 但 ``exercised=False``：**能力在，这次没驱动它**——空跑自检 /
      fixture 回放不连库，或者采集路径还没走到那一侧。

    三种都回空计数，但结论完全不同；合成一句「没有数据」会让第三种被读成「跑过了，结果是零」。
    """

    if not research_enabled:
        return {
            "status": research_status,
            "exercised": exercised,
            "counts": {},
            "total": 0,
        }
    counts = Counter(
        outcome.stop_reason for outcome in outcomes if outcome.stop_reason in STOP_REASONS
    )
    unknown = [
        outcome.stop_reason
        for outcome in outcomes
        if outcome.stop_reason is not None and outcome.stop_reason not in STOP_REASONS
    ]
    return {
        "status": research_status,
        "exercised": exercised,
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
        retrieved = {(hit.document, hit.section) for hit in outcome.hits if hit.section is not None}
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


def research_model_call_budget() -> tuple[int, int]:
    """一次 Research 的模型调用硬顶与**含兜底位**的硬顶：``(3, 4)``。

    **从代码常量读，不在这里抄一份**（#156）：#148 写的是「3 次」，#149 之后生成阶段多一个
    强模型失败的兜底位变成「最多 4 次」。报告、用例、脚本各处如果各写一个数字，迟早出现
    「脚本文案说 3、代码允许 4」这种两套口径——而它恰好是成本话题，读的人会当真。

    惰性导入：这个模块只依赖标准库，但 ``scripts/`` 的既有纪律是「dry-run 不 import 应用」，
    保持它能让空跑自检仍然只碰标准库。
    """

    from app.services.research_limits import MAX_MODEL_CALLS, MAX_MODEL_CALLS_WITH_FALLBACK

    return MAX_MODEL_CALLS, MAX_MODEL_CALLS_WITH_FALLBACK


def research_search_budget() -> int:
    """一次 Research 最多跑几条子检索（首轮 + 补证轮），从代码常量读。

    ``MAX_FIRST_ROUND_QUERIES + MAX_FOLLOWUP_ROUNDS × MAX_FOLLOWUP_QUERIES``。每条子检索是
    一次查询嵌入（重排开着再加一次），所以它是**预计调用次数**那本账里 Research 侧的第二个
    加数——只印 LLM 那三次会让预算看起来少一半（#153 的 400 次总预算就是这么来的）。
    """

    from app.services.research_limits import (
        MAX_FIRST_ROUND_QUERIES,
        MAX_FOLLOWUP_QUERIES,
        MAX_FOLLOWUP_ROUNDS,
    )

    return MAX_FIRST_ROUND_QUERIES + MAX_FOLLOWUP_ROUNDS * MAX_FOLLOWUP_QUERIES


def predict_calls(
    *,
    source: str,
    question_count: int,
    research_enabled: bool,
    research_status: str,
    rerank_enabled: bool = False,
) -> dict[str, Any]:
    """开跑前先算一遍：这次要花多少次调用（嵌入 + 重排 + Research 全算）。

    ``live`` 每题一次查询嵌入（改写关，所以向量一路只有原句），重排开着再加一次；``dry-run``
    / ``fixture`` 一次都不调。Research 侧分三种，报告里必须分开写（#156）：

    * ``research=off``：这次不要跑；
    * 能力没装（``capability-not-implemented``）：跑了也没东西可跑；
    * 能力已装但**这次不采集**（dry-run / fixture 不连库）：0 次是「没采集」，不是「免费」。
      这一条最容易读错，所以它连硬顶一起印出来。

    真的会跑 Research 时用**上限**估：每题 ≤ ``MAX_MODEL_CALLS_WITH_FALLBACK`` 次 LLM +
    ≤ ``research_search_budget()`` 条子检索 ×（嵌入 [+ 重排]）。上限而不是典型值，是因为
    「跑之前打印预计」的用途是**别超预算**；跑完再打实际值。
    """

    embedding = question_count if source == SOURCE_LIVE else 0
    rerank = question_count if (embedding and rerank_enabled) else 0
    research_llm = 0
    research_searches = 0
    calls, calls_with_fallback = research_model_call_budget()
    searches_cap = research_search_budget()
    will_run = (
        research_enabled and research_status == CAPABILITY_AVAILABLE and source == SOURCE_LIVE
    )
    if not research_enabled:
        research_note = "未启用（research=off）"
    elif research_status == CAPABILITY_NOT_IMPLEMENTED:
        # 能力没装：实际不会发出任何请求，也不按「将来会有 3 次」预扣。
        research_note = f"未落地（{CAPABILITY_NOT_IMPLEMENTED}），实际不会发出任何请求"
    elif will_run:
        research_llm = question_count * calls_with_fallback
        research_searches = question_count * searches_cap
        research_note = (
            f"会真的走 /rag/research：每题 ≤{calls_with_fallback} 次模型调用 + "
            f"≤{searches_cap} 条子检索（每条 1 次嵌入"
            f"{' + 1 次重排' if rerank_enabled else ''}），按上限估"
        )
    else:
        research_note = (
            f"能力已就位，但本次不采集（--source {source} 不连库），实际 0 次；"
            f"一次 Research 的硬顶是 {calls} 次模型调用（含生成阶段的强模型兜底位最多 "
            f"{calls_with_fallback} 次）"
        )
    research_total = research_llm + research_searches * (1 + (1 if rerank_enabled else 0))
    return {
        "embedding": embedding,
        "rerank": rerank,
        "research_llm": research_llm,
        "research_searches": research_searches,
        "total": embedding + rerank + research_total,
        "notes": {
            "embedding": (
                f"{question_count} 题 × 1 次查询向量（--source live，改写关）"
                if embedding
                else "不连库、不发网络（--source dry-run/fixture）"
            ),
            "rerank": (
                f"{question_count} 题 × 1 次重排（重排开着）"
                if rerank
                else "重排关着（或本次不连库）：这一层不发请求"
            ),
            "research_llm": research_note,
        },
        # 硬顶单独一栏：它是「一次 Research 最多烧几次」，与「这次跑打不打算调」无关。
        "research_llm_budget": {
            "hard_cap": calls,
            "with_strong_model_fallback": calls_with_fallback,
            "sub_searches": searches_cap,
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


def check_frozen(*, root: Path | None = None, manifest_path: Path | None = None) -> FrozenCheck:
    """拿清单与工作区对账。

    三类问题分开报而不是合成一个布尔：``missing`` 是「语料被删了」、``changed`` 是
    「内容被改了」、``extra`` 是「往里加了文档」——#138 验收第 1 条最担心的正是第三类
    （往里加文档等于基线当场失效），它必须能被单独看见。
    """

    base_root = root or REPO_ROOT
    path = manifest_path or (base_root / "eval/frozen-assets.sha256.json")
    if not path.is_file():
        raise SystemExit(
            f"缺哈希清单 {path}；先跑 `uv run python scripts/eval_refs.py freeze --write`"
        )
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
    #: ``auto`` / ``on`` / ``off``：图谱注入跟着重排走，所以它也是「这组对照成立不成立」的条件。
    rerank: str = RERANK_AUTO
    #: 数据面身份（Research 的门禁要读密钥配置与套餐）。Key 原文不进产物。
    api_key: str | None = None


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


def _graph_injection_wired() -> bool:
    """图谱注入**装没装**：注入服务在，且组合根收得下那个工厂。

    两个条件缺一不可——只有服务模块而没有缝（或反过来），都表示「这条路今天走不通」；
    只查配置开关是最容易糊弄过去的做法（开关开着 ≠ 代码里有这一步）。

    导入失败一律当「没装」：缺依赖 / 半合并的树上，探针不该让整份报告崩掉。
    """

    try:
        from app.services.graph_injection import build_graph_injection  # noqa: F401
        from app.services.rag_service import retrieve
    except Exception:  # noqa: BLE001 - 探针只回答「有没有」，不解释为什么没有
        return False
    return "graph_factory" in inspect.signature(retrieve).parameters


def _research_wired() -> bool:
    """Research **装没装**：门禁服务在，HTTP 路由也注册着。

    两个都要：只有服务而没有路由，调用方拿不到入口；只有路由而没有服务，那是一条会被
    ``ImportError`` 挡在半路的死路径。MCP 的第三个工具与 HTTP 共用同一个 ``gated_research``
    （ADR-0026 §1），所以服务在就代表两侧在。
    """

    try:
        from app.api.routes.rag import router
        from app.services.research_gate import gated_research
    except Exception:  # noqa: BLE001 - 探针只回答「有没有」
        return False
    if not callable(gated_research):
        return False
    return "/rag/research" in {getattr(route, "path", "") for route in router.routes}


def capability_status() -> dict[str, str]:
    """图谱与 Research 两侧**各自是什么状态**——探测代码，不读开关（#156）。

    两种取值：``available``（代码里真有这一步）与 ``capability-not-implemented``
    （没有）。**没有** ``disabled``：那是「开关关了」，属于 ``--graph`` / ``--research``
    的取值，不在这里出现——两个维度混成一个状态，就会出现「开关关了所以报未实现」这种
    自相矛盾的读数。

    这个函数**故意不收** :class:`RunConfig`：收了就会有人顺手读一个开关，
    而「能力在不在」与「这次想不想跑」是两条互不相干的事实。
    """

    return {
        "graph": CAPABILITY_AVAILABLE if _graph_injection_wired() else CAPABILITY_NOT_IMPLEMENTED,
        "research": (CAPABILITY_AVAILABLE if _research_wired() else CAPABILITY_NOT_IMPLEMENTED),
    }


def exercised_sides(
    *,
    config: RunConfig,
    capabilities: Mapping[str, str],
    rerank_enabled: bool,
) -> dict[str, bool]:
    """这次跑**有没有真的驱动**那一侧（不是「开关写的是什么」）。

    三个条件缺一不可，而且每一条都对应一个真实的失败形态：

    * ``source == live``：空跑自检 / fixture 回放不连库，开关打开也一次都没调；
    * 开关是 ``on``：这是「这次想不想跑」，关着就是不要；
    * 能力探测为 ``available``：代码里没有这一步时，开关只是个装饰。

    图谱还多一条 ``rerank_enabled``：注入项 ``score = 0`` 且追加在池尾，重排关着时必然被
    ``top_k`` 截掉，所以组合根**根本不装配**这一步（ADR-0027 开关归属）。那时报「已采集」
    就是在替一次没发生的事作证。
    """

    live = config.source == SOURCE_LIVE
    return {
        "graph": (
            live
            and config.graph == "on"
            and capabilities.get("graph") == CAPABILITY_AVAILABLE
            and rerank_enabled
        ),
        "research": (
            live
            and config.research == "on"
            and capabilities.get("research") == CAPABILITY_AVAILABLE
        ),
    }


def run_batch(config: RunConfig) -> dict[str, Any]:
    """一把跑完 8 道题，返回 ``results.json`` 的完整载荷。"""

    manifest = load_manifest()
    questions_payload = load_questions()
    questions: Sequence[dict[str, Any]] = questions_payload["questions"]

    index_versions: dict[str, str] = {}
    model_chain: dict[str, Any] = {}
    if config.source == SOURCE_DRY_RUN:
        outcomes = run_dry(questions=questions, top_k=config.top_k)
    elif config.source == SOURCE_FIXTURE:
        if config.fixtures is None:
            raise SystemExit("--source fixture 需要 --fixtures <文件>")
        outcomes = run_fixture(questions=questions, path=config.fixtures)
    else:
        # 模型接入先接到生产那条链**再**开跑：Research 的三步全走它，检索侧的嵌入 /
        # 重排不吃它（它们各有自己的端点）。不接的后果是整组读数落成 degraded，
        # 而那不是能力的结论（见 _bind_model_chain 的说明）。
        model_chain = asyncio.run(_bind_model_chain())
        outcomes = asyncio.run(
            run_live(
                questions=questions,
                kb_ids=config.kb_ids,
                top_k=config.top_k,
                tenant_id=config.tenant_id,
                graph=config.graph,
                research=config.research,
                rerank=config.rerank,
                api_key=config.api_key,
            )
        )
        # 「这一次查的是哪一次构建」：报告的每个数字都要能对到某个版本上（ADR-0025）。
        index_versions = asyncio.run(
            active_index_versions(config.kb_ids, tenant_id=config.tenant_id)
        )

    statuses = capability_status()
    rerank_enabled = _effective_rerank(config)
    research_enabled = config.research == "on"
    research_status = statuses["research"] if research_enabled else "disabled"
    exercised = exercised_sides(config=config, capabilities=statuses, rerank_enabled=rerank_enabled)
    trace = citation_traceability(outcomes, manifest=manifest)
    coverage = coverage_by_question(outcomes, questions)

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": config.source,
        "switches": {
            "graph": config.graph,
            "research": config.research,
            # 重排是图谱的前置（注入跟着重排走）：不记它，on/off 那两组读数就少了前提。
            "rerank": config.rerank,
        },
        "rerank_effective": rerank_enabled,
        "capabilities": dict(statuses),
        # 与 capabilities 并列、不合并：前者是「代码里有没有」，这里是「这次跑有没有走那条路」
        # （#156）。两份事实都要进 results.json，否则事后分不清「没采集」与「没能力」。
        "exercised": exercised,
        "index_versions": dict(sorted(index_versions.items())),
        # 这一轮 Research 用的是哪条模型接入（``model_configs`` 的角色指针 / 回落 .env）。
        # 它不是能力读数，而是「这组数字在什么前提下调出来的」——不记它，degraded 的那一组
        # 事后无法归因到「评测没接生产链」还是「Research 本身不行」。
        "llm_chain": dict(model_chain),
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
            rerank_enabled=rerank_enabled,
        ),
        "metrics": {
            "stop_reason_distribution": stop_reason_distribution(
                outcomes,
                research_enabled=research_enabled,
                research_status=research_status,
                exercised=exercised["research"],
            ),
            "retrieval_termination": retrieval_termination(outcomes),
            "citation_traceability": {
                "hits": trace.hits,
                "traceable": trace.traceable,
                "rate": trace.rate,
                "untraceable": trace.untraceable,
            },
            # Research 侧的两样机械读数：完整度分布与引用块自检。没有一块引用时
            # ``evidence_blocks`` 是 0 而不是 None——「没有块」本身就是一条读数。
            "research": research_metrics(outcomes),
            "graph_injection": graph_metrics(outcomes),
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
                "evidence_state": outcome.evidence_state,
                "citation_blocks": outcome.citation_blocks,
                "citations_with_evidence": outcome.citations_with_evidence,
                "dangling_evidence_ids": outcome.dangling_evidence_ids,
                "graph_injected": outcome.graph_injected,
                "graph_seeds": outcome.graph_seeds,
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


def research_metrics(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Research 侧两样机械可核验读数：``evidence_state`` 分布 + 引用块自检。

    引用块自检回答「每块引用点了材料里的哪几条」：块数、带证据的块数、指向材料之外的下标。
    它**不做答案质量**（#138 Out of Scope），只回答「引用能不能还原到材料」。
    """

    states = Counter(
        outcome.evidence_state for outcome in outcomes if outcome.evidence_state is not None
    )
    blocks = sum(outcome.citation_blocks for outcome in outcomes)
    with_evidence = sum(outcome.citations_with_evidence for outcome in outcomes)
    dangling = [
        {"question": outcome.id, "evidence_ids": outcome.dangling_evidence_ids}
        for outcome in outcomes
        if outcome.dangling_evidence_ids
    ]
    return {
        "evidence_state": dict(sorted(states.items())),
        "questions_with_evidence_state": sum(states.values()),
        "evidence_blocks": blocks,
        "blocks_with_evidence": with_evidence,
        "dangling_evidence": dangling,
    }


def graph_metrics(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """图谱注入的读数：注入了几条、从几个种子扩出去（只统计**有这一栏**的题）。

    ``questions_with_injection`` 与 ``injected`` 分开：前者是「这一栏出现了几次」，
    后者是「一共补了几条候选」。关掉图谱时两者都是 0，而 ``graphs_reported`` 为 0
    说明这一栏压根没出现——那才是「没有这一步」，与「有这一步但没补到」不是一件事。
    """

    reported = [outcome for outcome in outcomes if outcome.graph_injected is not None]
    return {
        "graphs_reported": len(reported),
        "injected": sum(outcome.graph_injected or 0 for outcome in reported),
        "seeds": sum(outcome.graph_seeds or 0 for outcome in reported),
        "questions_with_injection": sum(
            1 for outcome in reported if (outcome.graph_injected or 0) > 0
        ),
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


def _graph_note(status: str) -> str:
    """图谱那一栏的说明：**状态说什么就写什么**，不写死的「还没落地」。"""

    if status == CAPABILITY_AVAILABLE:
        return (
            "注入服务已装配（`app/services/graph_injection.py` + 组合根的 `graph_factory`）；"
            "注入项 score=0、按本次请求的库与版本过滤，名次交给重排"
        )
    return "注入服务或组合根那条缝不在（`graph_factory` 收不到），这一次不会有任何图谱候选进池"


def _research_note(status: str) -> str:
    """Research 那一栏的说明；硬顶从代码常量取，避免「报告说 3、代码允许 4」。"""

    calls, with_fallback = research_model_call_budget()
    if status == CAPABILITY_AVAILABLE:
        return (
            "`/rag/research` 与 MCP `rag_research` 已就位（同一份 `gated_research`）；"
            f"一次调用硬顶 {calls} 次模型调用（含生成兜底 {with_fallback} 次）"
        )
    return "`/rag/research` 或 `research_gate` 不在——七种停止原因、引用块四条硬校验一条都不存在"


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
    add(
        f"- 开关：graph={config.graph} / research={config.research} / "
        f"rerank={config.rerank}（实际 {'开' if payload.get('rerank_effective') else '关'}）"
    )
    if payload.get("index_versions"):
        add(
            "- 索引版本（这一次查的是哪一次构建）："
            + "、".join(
                f"`{kb_id}` → `{version or '未解析'}`"
                for kb_id, version in payload["index_versions"].items()
            )
        )
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
        f"嵌入 {predicted['embedding']} + 重排 {predicted['rerank']} + "
        f"Research {predicted['research_llm']} 次 LLM 与 ≤{predicted['research_searches']} 条子检索）"
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
    add(
        "Research 的七种固定停止原因（#138 §C）：" + "、".join(f"`{name}`" for name in STOP_REASONS)
    )
    add("")
    if distribution["status"] == CAPABILITY_NOT_IMPLEMENTED:
        add(
            f"**{CAPABILITY_NOT_IMPLEMENTED}**：代码里没有这条路（`app.services.research_gate` "
            "或 `/rag/research` 路由缺失），所以七种原因一种都没有出现——这不是「都试过了，"
            "结果是这些」。"
        )
    elif distribution["status"] == "disabled":
        add(
            "**未启用**（research=off）：本次不跑 Research，七种原因不适用。"
            "要读这一侧，加 `--research on`。"
        )
    elif not distribution["exercised"]:
        add(
            f"**能力已就位，但这次没有采集这一侧**（`{distribution['status']}`，"
            f"`exercised=false`）：`/rag/research` 与 MCP `rag_research` 都在，但这一轮没有"
            f"真的驱动它——`--source {payload['source']}` 不连库（空跑自检 / fixture 回放）时"
            "一次都不会调，所以七种原因**本报告不产出**。这不是「跑完了、一条都没有」。"
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

    # Research 侧的两样机械读数：只在真的采集过这一侧时才有意义——否则全是 0，而 0 会被
    # 读成「跑过了、一块引用都没有」（与「没采集」正好相反）。
    research = metrics.get("research") or {}
    if payload["exercised"].get("research"):
        add("### 引用块完整度（`evidence_state`）")
        add("")
        add(
            "完整度由**核心片数量**算出来、不由模型自评（#138 §B）：`complete` = 每个要点都有"
            "核心片、`partial` = 部分有、`missing` = 都没有。引用块自检只回答「这一块点的证据"
            "下标在不在材料里」，**不做答案质量**（#138 Out of Scope）。"
        )
        add("")
        add("| 完整度 | 题数 |")
        add("| --- | --- |")
        for name, count in research.get("evidence_state", {}).items():
            add(f"| `{name}` | {count} |")
        if not research.get("evidence_state"):
            add("| （无） | 0 |")
        add("")
        add("| 引用块自检 | 值 |")
        add("| --- | --- |")
        add(f"| 引用块数 | {research.get('evidence_blocks', 0)} |")
        add(f"| 带证据下标的块 | {research.get('blocks_with_evidence', 0)} |")
        add(f"| 指向材料之外的下标（题数） | {len(research.get('dangling_evidence') or [])} |")
        add("")
        if research.get("dangling_evidence"):
            add(
                "> 有引用块点了材料集合之外的 evidence 下标：那是「引用可回溯」的反面，"
                "按 #138 §C 的四条硬校验本不该发生——记下来，别当没看见。"
            )
            add("")

    # 图谱注入的读数同理：只在真的采集过 graph 那一侧时展开。
    graph = metrics.get("graph_injection") or {}
    if payload["exercised"].get("graph"):
        add("### 图谱注入（`metadata.retrieval.graph`）")
        add("")
        add(
            "注入项 `score = 0`、追加在候选池尾部，名次交给重排（ADR-0027 决策 4）："
            "「补进来了几条」与「最后有没有入选 top_k」是两件事，这里只报前者。"
        )
        add("")
        add("| 指标 | 值 |")
        add("| --- | --- |")
        add(f"| 有这一栏的题 | {graph.get('graphs_reported', 0)} |")
        add(f"| 注入候选合计 | {graph.get('injected', 0)} |")
        add(f"| 种子节合计 | {graph.get('seeds', 0)} |")
        add(f"| 有注入的题 | {graph.get('questions_with_injection', 0)} |")
        add("")
        if not graph.get("injected"):
            add(
                "> 这一侧跑了却一条候选都没补：先查这个库在本版本里有没有边"
                "（`section_edges`），再查种子节的 `section_id` 是不是空的"
                "（无标题文档没有节级关系可扩）。"
            )
            add("")

    add("### 今天链路的终止形态（等价物，**不是**停止原因）")
    add("")
    add(
        "上一节是 Research 状态机的出口；本脚本跑的是检索内核（一次返回），"
        "它的终止形态只有这几种。两者并列展示，谁都不冒充谁。"
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
    add(
        f"| 开跑前预计 | {predicted['total']}（嵌入 {predicted['embedding']}、"
        f"重排 {predicted['rerank']}、Research {predicted['research_llm']} 次 LLM"
        f" + ≤{predicted['research_searches']} 条子检索） |"
    )
    add("")
    add(f"- 嵌入：{predicted['notes']['embedding']}")
    add(f"- 重排：{predicted['notes']['rerank']}")
    add(f"- Research：{predicted['notes']['research_llm']}")
    add(
        "- 口径：每次检索 = 1 次查询嵌入 + （响应里有重排事实时）1 次重排；"
        "一次 Research = `metadata.model_calls` 次 LLM + `rounds.searches` 条子检索 × "
        "（嵌入 [+ 重排]）。**按响应里的事实数，不按配置估**。"
    )
    add("")

    add("## 能力开关状态")
    add("")
    add(
        "两栏分开读：**状态**是探测代码得出的（这台机器上有没有这一步），**这次跑**是开关"
        "与脚本接线共同决定的（这次有没有真的走那条路）。两者不是同一件事（#156）。"
    )
    add("")
    add("| 开关 | 本次取值 | 状态（探测） | 这次跑 | 说明 |")
    add("| --- | --- | --- | --- | --- |")
    add(
        f"| graph | {config.graph} | `{statuses['graph']}` "
        f"| {'已采集' if payload['exercised']['graph'] else '**未采集**'} "
        f"| {_graph_note(statuses['graph'])} |"
    )
    add(
        f"| research | {config.research} | `{statuses['research']}` "
        f"| {'已采集' if payload['exercised']['research'] else '**未采集**'} "
        f"| {_research_note(statuses['research'])} |"
    )
    add("")
    unexercised = [
        name
        for name in ("graph", "research")
        if getattr(config, name) == "on" and not payload["exercised"][name]
    ]
    if unexercised:
        reasons = []
        if payload["source"] != SOURCE_LIVE:
            reasons.append(f"`--source {payload['source']}` 不连库")
        if payload.get("rerank_effective") is False:
            reasons.append("重排关着（图谱注入跟着重排走，组合根不装配）")
        add(
            "> **对照有效性**：本次有开关被打开（"
            + "、".join(f"`{name}`" for name in unexercised)
            + "），但这次没有真的驱动那一侧"
            + ("：" + "；".join(reasons) if reasons else "")
            + "。所以 on / off 两组读数会是同一组数——这是「没采集」，不是「接了没用」。"
        )
        add("")
    not_ready = [name for name in ("graph", "research") if statuses[name] != CAPABILITY_AVAILABLE]
    if not_ready:
        add(
            "> **未就位的两侧**："
            + "、".join(f"`{name}`" for name in not_ready)
            + f" 探测结果为 `{CAPABILITY_NOT_IMPLEMENTED}`（代码里没有这一步），"
            "即便打开开关也不会有读数。"
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
    add(
        'uv run python scripts/eval_refs.py verify-citation --document points_policy --section 2.2 --snippet "银卡及以上时，200 元的固定上限提高到 500 元"'
    )
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
    """开跑前把预计调用次数印出来——花不花钱要在按下回车之前就知道。

    三行分开（嵌入 / 重排 / Research）：合起来一个数字之后，「重排关着所以图谱没跑」
    这类前提就没地方写了，而它恰好决定这组对照成不成立。
    """

    print("预计模型调用次数：")
    print(f"  嵌入      : {predicted['embedding']} 次 — {predicted['notes']['embedding']}")
    print(f"  重排      : {predicted['rerank']} 次 — {predicted['notes']['rerank']}")
    print(
        f"  Research  : {predicted['research_llm']} 次 LLM"
        f"（+ 子检索 ≤{predicted['research_searches']} 条）"
        f" — {predicted['notes']['research_llm']}"
    )
    print(f"  合计      : {predicted['total']} 次")


def _command_run(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get(ENV_API_KEY) or None
    config = RunConfig(
        source=args.source,
        graph=args.graph,
        research=args.research,
        allow_model_calls=args.allow_model_calls,
        kb_ids=tuple(args.kb_ids),
        top_k=args.top_k,
        fixtures=Path(args.fixtures) if args.fixtures else None,
        tenant_id=args.tenant_id,
        rerank=args.rerank,
        api_key=api_key,
    )
    if config.source == SOURCE_LIVE and not config.allow_model_calls:
        # 真实链路每题要烧一次查询嵌入。没有显式参数就不发请求——默认值站在「不花钱」这边。
        print(
            "拒绝执行：--source live 会真的调用嵌入 Provider（每题 1 次）。\n"
            "确需花这笔钱时显式加 --allow-model-calls；想先看结构就跑默认的空跑自检。",
            file=sys.stderr,
        )
        return 2
    if config.source == SOURCE_LIVE and config.research == "on" and not config.api_key:
        # 门禁要读密钥配置与套餐：没有身份就没有「这个租户买了什么」，整组对照不成立。
        # 只对 live 拦：空跑自检不连库，也就没有身份可言（它连门禁都不会碰）。
        print(
            f"--research on 需要 --api-key（或环境变量 {ENV_API_KEY}）：Research 的三层授权"
            "（白名单 / 密钥配置 / 套餐）都挂在数据面身份上。",
            file=sys.stderr,
        )
        return 2

    questions = load_questions()["questions"]
    _print_predicted(
        predict_calls(
            source=config.source,
            question_count=len(questions),
            research_enabled=config.research == "on",
            research_status=capability_status()["research"],
            rerank_enabled=_effective_rerank(config),
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
    research = payload["metrics"]["research"]
    if payload["exercised"]["research"]:
        print(
            f"Research：引用块 {research['evidence_blocks']} 个"
            f"（带证据 {research['blocks_with_evidence']}），"
            f"完整度 {research['evidence_state'] or '（无）'}，"
            f"悬空证据下标 {len(research['dangling_evidence'])} 题"
        )
    print(f"产物：{target / 'report.md'}（结构同目录 results.json）")
    return 0


def _command_verify_citation(args: argparse.Namespace) -> int:
    result = resolve_citation(document=args.document, section=args.section, snippet=args.snippet)
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
    run.add_argument("--research", choices=("off", "on"), default="off", help="Research 侧（#148）")
    run.add_argument(
        "--rerank",
        choices=RERANK_OPTIONS,
        default=RERANK_AUTO,
        help=(
            "重排开关（默认 auto = 跟服务端配置走）。图谱注入跟着重排走（ADR-0027），"
            "所以这一栏是 on/off 那组对照成立的前提，也会写进报告"
        ),
    )
    run.add_argument(
        "--api-key",
        default=None,
        help=(
            "数据面 API Key（也可用环境变量 "
            f"{ENV_API_KEY}）。--research on 必须有它：门禁要读密钥配置与套餐"
        ),
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
    if sys.platform == "win32" and args.command == "run":
        # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
        # InterfaceError）。空跑 / 回放不碰数据库，但这条线一旦切到 ``--source live``
        # 就是第一条查询：与 scripts/rebuild_kb_version.py、conftest 同一行理由。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
