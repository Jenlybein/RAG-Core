"""#140 · 检索内核：召回 → 融合 →（注入位）→ 重排 → TopK → 折叠。

接缝只有一处：``app.services.retrieval.kernel.retrieve_kernel``。它**不是**一个新的
内部实现细节，而是 ADR-0028 定下的边界本身——``/rag/retrieve`` 与将来的受控 Research
共用这一份实现，所以它必须能被单独调用、单独断言。

本文件守内核自己的四条边界（四条与响应字节无关，应答第 5 条由
``tests/test_multi_query_union.py`` 的黄金哈希守着）：

1. **签名显式**：租户、``kb_id → version`` 范围、原句与三条查询串、条数与深度、
   重排开关与工厂、计时器、注入位——全部由调用方递进来，内核不读全局配置；
2. **内核不写归属索引、不记账、不发观测**：这些是「每次调用只做一次」的事（ADR-0028
   判据：每条子查询各做一次 → 内核；每次调用只做一次 → 外层）；
3. **物理边界**：同一请求连续调两次内核（模拟多查询）既不落行也不发 trace，而外层
   一次调用仍然只落一行、只发一份；
4. **取消能穿透内核**：客户端断开时正在 ``await`` 的嵌入当场收到 ``CancelledError``。

数据库与三类 Provider 一律离线替身：不连 PG、不连 ES、不发网络。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.observability import reset_trace_sink, set_trace_sink
from app.observability.stage_timing import (
    STAGE_COLLAPSE,
    STAGE_FUSION,
    STAGE_KEYWORD_RECALL,
    STAGE_RERANK,
    STAGE_VECTOR_RECALL,
    StageTimer,
)
from app.services import result_collapse
from app.services.retrieval_options import RetrievalOptions
from tests.fake_db import AsyncRecordingSession, AsyncSessionAdapter

TENANT = "tenant_kernel"
KB_A = "kb-a"
KB_B = "kb-b"
QUERY = "退款多久到账"

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_PATH = REPO_ROOT / "app" / "services" / "retrieval" / "kernel.py"

#: 内核源码里一个都不许出现的名字。它们全是「外层各做一次」那五件事的入口
#: （ADR-0028 §2）：改写、可见性、配额、归属索引、观测；``settings`` 是第六条——
#: 内核只吃调用方递进来的参数，自己去问配置就等于范围有了两个来源。
_FORBIDDEN_IN_KERNEL: tuple[str, ...] = (
    "settings",
    "optimize_query",
    "ensure_visible",
    "record_retrieval_trace",
    "emit_retrieval_trace_off_loop",
    "quota_gate",
)


# ─────────────────────────────────────────────────────────────── 离线替身


class _RecordingEmbedding:
    """查询侧嵌入替身：记下句子，返回一个可辨认的向量。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class _RecordingVectorStore:
    """向量召回替身：记下每次的**范围**与**深度**，范围只能从映射里读出来。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else [_vector_row("c1", score=0.9)]
        self.scopes: list[dict[str, str]] = []
        self.depths: list[int] = []

    async def similarity_search_scope(
        self, db: Any, query_vector: Any, *, tenant_id: str, index_versions: Any, top_k: int
    ) -> list[dict]:
        self.scopes.append(dict(index_versions))
        self.depths.append(top_k)
        kb_id = next(iter(index_versions))
        # 行自带 ``kb_id`` 时以它为准（范围是映射，一个查询可以覆盖多个库）。
        return [dict(row) if row.get("kb_id") else dict(row, kb_id=kb_id) for row in self.rows][
            :top_k
        ]

    async def similarity_search(self, *args: Any, **kwargs: Any) -> list[dict]:  # pragma: no cover
        raise AssertionError("检索走范围版，不许按库循环")


class _RecordingKeyword:
    available = True

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else [_keyword_row("c1")]
        self.scopes: list[dict[str, str]] = []
        self.queries: list[str] = []

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: Any, top_k: int
    ) -> list[dict]:
        self.queries.append(query)
        self.scopes.append(dict(index_versions))
        return [dict(row) for row in self.rows][:top_k]


class _StubRerank:
    """重排替身：可用，原样返回（这一步真的走过就够了）。"""

    available = True
    provider_name = "stub"
    model_name = None

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        self.calls += 1
        return list(chunks)[:top_n]


async def _async_value(value: Any) -> Any:
    """协程工厂的替身外壳（注入缝与生产工厂同形，见 #137）。"""

    return value


class _BlockingEmbedding:
    """卡住直到被取消的嵌入：取消有没有**传到内核里这一段**是可观察的事实。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def embed_query(self, query: str) -> list[float]:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [0.1, 0.2, 0.3]  # pragma: no cover - 走不到：上面必然被取消


class _RecordingSink:
    """观测去向替身：只数「有没有人往里发东西」。"""

    def __init__(self) -> None:
        self.traces: list[str] = []

    def record_trace(self, trace_id: str, attributes: Any, *, query: str | None = None) -> None:
        self.traces.append(trace_id)

    def record_span(self, trace_id: str, name: str, duration_ms: float) -> None:
        return None

    def record_score(self, trace_id: str, score: int, *, comment: str | None = None) -> None:
        return None

    def flush(self, trace_id: str) -> None:
        return None


def _vector_row(
    chunk_id: str,
    *,
    score: float = 0.9,
    document_id: str = "doc-1",
    kb_id: str | None = None,
) -> dict:
    row: dict[str, Any] = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "title": "退款政策",
        "content": f"content-{chunk_id}",
        "score": score,
    }
    if kb_id is not None:
        row["kb_id"] = kb_id
    return row


def _keyword_row(chunk_id: str, *, score: float = 9.0) -> dict:
    return {**_vector_row(chunk_id), "score": score}


# ─────────────────────────────────────────────────────────── 内核调用助手


def _kernel_kwargs(**overrides: Any) -> dict[str, Any]:
    """一份最小的内核入参：单模式、不折叠、不重排。"""

    kwargs: dict[str, Any] = {
        "tenant_id": TENANT,
        "index_versions": {KB_A: DEFAULT_INDEX_VERSION},
        "query": QUERY,
        "mode": "vector",
        "vector_queries": (QUERY,),
        "rerank_query": QUERY,
        "keyword_query": QUERY,
        "top_k": 2,
        "recall_top_k": 2,
        "rerank_enabled": False,
        "fold_to_sections": False,
        "expand_table_rows": False,
    }
    kwargs.update(overrides)
    return kwargs


async def _run_kernel(
    *,
    db: Any = None,
    embedding: Any = None,
    vector: Any = None,
    keyword: Any = None,
    **overrides: Any,
) -> tuple[Any, SimpleNamespace]:
    """直接调内核一次；返回结果与三个替身（断言打在替身上，不打断言内核内部）。"""

    kernel = importlib.import_module("app.services.retrieval.kernel")
    embedding = embedding if embedding is not None else _RecordingEmbedding()
    vector = vector if vector is not None else _RecordingVectorStore()
    keyword = keyword if keyword is not None else _RecordingKeyword()

    outcome = await kernel.retrieve_kernel(
        db if db is not None else AsyncRecordingSession(),
        **_kernel_kwargs(
            embedding_factory=lambda: embedding,
            vector_factory=lambda: vector,
            keyword_factory=lambda: keyword,
            **overrides,
        ),
    )
    return outcome, SimpleNamespace(embedding=embedding, vector=vector, keyword=keyword)


# ─────────────────────────────────────── 一、签名：一切都是显式递进来的


def test_the_kernel_takes_the_scope_queries_switches_and_timer_explicitly() -> None:
    """内核签名就是 ADR-0028 的边界：范围、查询串、条数、开关、计时器、注入位全显式。"""

    kernel = importlib.import_module("app.services.retrieval.kernel")
    signature = inspect.signature(kernel.retrieve_kernel)
    parameters = signature.parameters

    required = {
        "tenant_id",
        "index_versions",
        "query",
        "mode",
        "vector_queries",
        "rerank_query",
        "keyword_query",
        "top_k",
        "recall_top_k",
        "recall_depth",
        "rerank_enabled",
        "rerank_candidates",
        "rerank_factory",
        "fold_to_sections",
        "expand_table_rows",
        "stage_timer",
        "embedding_factory",
        "vector_factory",
        "keyword_factory",
    }
    assert required <= set(parameters), f"内核签名缺参数：{sorted(required - set(parameters))}"
    assert parameters["graph_injection"].default is None, "注入位必须默认 None（本票无行为）"
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in parameters.items()
        if name != "db"
    ), "除 db 外全部只许按名传：位置参数在这里一定会传错"
    assert parameters["db"] is not None


def _code_names(path: Path) -> list[tuple[int, str]]:
    """文件里**代码**用到的名字（``(行号, 名字)``）：注释与字符串不算。"""

    names: list[tuple[int, str]] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.NAME:
                names.append((token.start[0], token.string))
    return names


def test_the_kernel_source_never_reaches_for_the_outer_five() -> None:
    """改写 / 可见性 / 记账 / 归属索引 / 观测 —— 内核的代码里一个入口都不许出现。"""

    offenders = [
        f"app/services/retrieval/kernel.py:{number}: {name}"
        for number, name in _code_names(KERNEL_PATH)
        if name in _FORBIDDEN_IN_KERNEL
    ]
    assert offenders == [], "内核里出现了外层的事：\n" + "\n".join(offenders)


def test_the_kernel_does_not_read_global_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """把服务端旋钮改到荒谬值，内核的召回深度仍只由入参决定。

    ``HYBRID_RECALL_TOP_K`` / ``RERANK_MAX_CANDIDATES`` 是**组合根**读的旋钮
    （它们不是每次调用都能选的）；内核自己去读一次，这两处就会有第二个来源，
    而两处不一致时「这次到底召回了多深」谁也说不清。
    """

    monkeypatch.setattr(settings, "HYBRID_RECALL_TOP_K", 9999)
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", 9999)

    async def go() -> SimpleNamespace:
        outcome, stubs = await _run_kernel(mode="hybrid", recall_top_k=3, recall_depth=None)
        assert outcome.chunks, "这次检索必须有结果，否则断言是空断言"
        return stubs

    stubs = asyncio.run(go())

    assert stubs.vector.depths == [3], "召回深度只许来自 recall_top_k / recall_depth"
    assert stubs.keyword.queries == [QUERY]


def test_the_scope_mapping_is_what_both_recall_paths_receive() -> None:
    """``kb_id → version`` 映射原样交给两路召回：范围只有这一个来源，且不是 ``v1`` 焊死的。"""

    scope = {KB_A: "v7", KB_B: "v8"}

    async def go() -> SimpleNamespace:
        outcome, stubs = await _run_kernel(
            mode="hybrid", index_versions=scope, recall_top_k=2, recall_depth=2
        )
        assert outcome.chunks
        return stubs

    stubs = asyncio.run(go())

    assert stubs.vector.scopes == [scope], "向量一路拿到的范围必须就是本次的映射"
    assert stubs.keyword.scopes == [scope], "关键词一路同理"


# ───────────────────────────────── 二、六段耗时：段名与顺序由内核这一层记下


def test_the_kernel_records_the_stage_names_in_link_order() -> None:
    """混合 + 重排：内核记下五段，顺序就是链路顺序；``query_optimization`` 在外层。"""

    timer = StageTimer()

    async def go() -> None:
        await _run_kernel(
            mode="hybrid",
            recall_top_k=2,
            recall_depth=2,
            rerank_enabled=True,
            rerank_factory=lambda: _async_value(_StubRerank()),
            stage_timer=timer,
        )

    asyncio.run(go())

    assert [span.name for span in timer.spans()] == [
        STAGE_VECTOR_RECALL,
        STAGE_KEYWORD_RECALL,
        STAGE_FUSION,
        STAGE_RERANK,
        STAGE_COLLAPSE,
    ]


def test_a_single_mode_kernel_records_only_the_stages_it_walks() -> None:
    """单模式没有融合、没有重排：那两段就不该出现，也不许补 0。"""

    bm25_timer = StageTimer()
    vector_timer = StageTimer()

    async def go() -> None:
        await _run_kernel(mode="bm25", keyword=_RecordingKeyword(), stage_timer=bm25_timer)
        await _run_kernel(mode="vector", stage_timer=vector_timer)

    asyncio.run(go())

    assert [span.name for span in bm25_timer.spans()] == [STAGE_KEYWORD_RECALL, STAGE_COLLAPSE]
    assert [span.name for span in vector_timer.spans()] == [STAGE_VECTOR_RECALL, STAGE_COLLAPSE]


# ─────────────────────────────────────────── 三、折叠在内核里（TopK 之后）


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FoldSession:
    """折叠路径的会话替身：第一次是 meta 回查，之后每次是一批单元行。

    按调用顺序回答（不是解析 SQL）：多查一次会立刻表现为 IndexError，而不是静默通过。
    ``scopes`` 记下每次单元回查带的参数——「按版本分组」这件事因此可观察。
    """

    def __init__(
        self, *, metas: dict[tuple[str, str], dict], units: dict[str, list[tuple]]
    ) -> None:
        self.metas = metas
        self.units = units
        self.scopes: list[dict[str, Any]] = []

    def execute(self, statement: Any) -> Any:
        if not self.scopes:
            self.scopes.append({"kind": "meta"})
            return _Rows(
                [
                    SimpleNamespace(id=chunk_id, kb_id=kb_id, meta=meta)
                    for (kb_id, chunk_id), meta in self.metas.items()
                ]
            )
        compiled = statement.compile()
        self.scopes.append({"kind": "units", "params": dict(compiled.params)})
        version = next(
            value
            for value in compiled.params.values()
            if isinstance(value, str) and value in self.units
        )
        return _Rows(self.units[version])


def _meta(heading: str, **extra: Any) -> dict:
    base = {"heading_path": heading, "chunk_type": "prose", "page": None, "doc_version": None}
    base.update(extra)
    return base


def test_the_kernel_delivers_whole_sections_after_top_k() -> None:
    """两条命中落在同一节 → 内核交回**一条**整节（ADR-0013）：折叠是内核的最后一步。"""

    heading = "退款政策 > 处理流程"
    session = _FoldSession(
        metas={("kb-a", "c-1"): _meta(heading), ("kb-a", "c-2"): _meta(heading)},
        units={
            DEFAULT_INDEX_VERSION: [
                ("doc-1", heading, 0, "第一句。", "c-1", "prose", 0),
                ("doc-1", heading, 1, "第二句。", "c-2", "prose", 0),
            ]
        },
    )

    async def go() -> Any:
        outcome, _stubs = await _run_kernel(
            db=AsyncSessionAdapter(session),
            vector=_RecordingVectorStore(
                [_vector_row("c-1", score=0.9), _vector_row("c-2", score=0.8)]
            ),
            top_k=1,
            fold_to_sections=True,
        )
        return outcome

    outcome = asyncio.run(go())

    assert len(outcome.chunks) == 1, "同一节的两条命中只占一个名额"
    unit = outcome.chunks[0]
    assert unit["chunk_id"] == "c-1"
    assert unit["section_id"] == result_collapse.unit_id_of(heading)
    assert unit["source_chunk_ids"] == ["c-1", "c-2"]
    assert unit["content"] == "第一句。\n\n第二句。"


def test_the_section_lookup_is_scoped_per_index_version() -> None:
    """两份文档属于两个版本：单元行回查必须**按版本各查一次**，不许混成一个池。

    这是索引即构建产物（#141）那条一致性前提在折叠侧的入口：把两个版本的行查进
    同一个池，重建中途的检索就会拿到「两套切法」拼起来的正文。
    """

    heading = "退款政策 > 处理流程"
    session = _FoldSession(
        metas={
            ("kb-a", "c-a"): _meta(heading),
            ("kb-b", "c-b"): _meta(heading),
        },
        units={
            "v1": [("doc-a", heading, 0, "甲。", "c-a", "prose", 0)],
            "v2": [("doc-b", heading, 0, "乙。", "c-b", "prose", 0)],
        },
    )

    async def go() -> Any:
        outcome, _stubs = await _run_kernel(
            db=AsyncSessionAdapter(session),
            index_versions={KB_A: "v1", KB_B: "v2"},
            vector=_RecordingVectorStore(
                [
                    _vector_row("c-a", score=0.9, document_id="doc-a", kb_id=KB_A),
                    _vector_row("c-b", score=0.8, document_id="doc-b", kb_id=KB_B),
                ]
            ),
            top_k=2,
            fold_to_sections=True,
        )
        return outcome

    outcome = asyncio.run(go())

    unit_scopes = [scope for scope in session.scopes if scope["kind"] == "units"]
    assert len(unit_scopes) == 2, "两个版本就该有两次回查（同版本仍是一次）"
    versions = sorted(
        value
        for scope in unit_scopes
        for value in scope["params"].values()
        if value in ("v1", "v2")
    )
    assert versions == ["v1", "v2"]
    documents = sorted(
        tuple(sorted(value))
        for scope in unit_scopes
        for value in scope["params"].values()
        if isinstance(value, list)
    )
    assert documents == [("doc-a",), ("doc-b",)], "两次回查各自只该带上自己那个版本的文档"
    assert {unit["content"] for unit in outcome.chunks} == {"甲。", "乙。"}


# ──────────────────────────── 四、内核没有副作用：写库、记账、观测都不在这里


def test_calling_the_kernel_twice_writes_nothing_and_emits_nothing() -> None:
    """同一请求连调两次内核：只有读语句，没有归属索引行，没有 trace。

    这是「一次 Research 只有一行归属索引、一份 trace」在代码形状上的前提：
    内核被调 N 次，写入次数不能跟着变成 N。
    """

    sink = _RecordingSink()
    set_trace_sink(sink)
    session = AsyncRecordingSession()
    embedding = _RecordingEmbedding()
    try:

        async def go() -> tuple[Any, Any, SimpleNamespace]:
            first, stubs = await _run_kernel(db=session, embedding=embedding)
            second, _stubs = await _run_kernel(db=session, embedding=embedding)
            return first, second, stubs

        first, second, stubs = asyncio.run(go())
    finally:
        reset_trace_sink()

    assert first.chunks == second.chunks, "两次同样的入参必须得到同样的结果（没有藏起来的全局状态）"
    assert stubs.embedding.queries == [QUERY, QUERY], "内核必须真的跑了两遍，否则断言是空断言"
    assert [row for row in session.added] == [], "内核不许往会话里写任何行"
    assert all(
        statement.lstrip().upper().startswith("SELECT") for statement in session.statements
    ), "内核只许读：写归属索引是外层的事"
    assert sink.traces == [], "内核不许发观测：一份 trace 是外层的事"


@pytest.mark.asyncio
async def test_one_request_keeps_one_row_and_one_trace_even_if_the_kernel_runs_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外层各做一次：即使这一次调用里内核跑了两遍（模拟多查询），记账与 trace 仍各一份。"""

    service = importlib.import_module("app.services.rag_service")
    real_kernel = service.retrieve_kernel
    stubs = _install_retrieve_stubs(monkeypatch)

    async def _twice(db: Any, **kwargs: Any) -> Any:
        first = await real_kernel(db, **kwargs)
        await real_kernel(db, **kwargs)  # 第二条子查询（Research 的形状）
        return first

    monkeypatch.setattr(service, "retrieve_kernel", _twice)

    recorded: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        recorded.append(kwargs)

    async def _emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(service, "record_retrieval_trace", _record)
    monkeypatch.setattr(service, "emit_retrieval_trace_off_loop", _emit)

    await service.retrieve(
        AsyncRecordingSession(),
        tenant_id=TENANT,
        kb_ids=[KB_A],
        query=QUERY,
        options=_options(),
    )

    assert stubs["embedding"].queries == [QUERY, QUERY], "内核确实跑了两遍"
    assert len(recorded) == 1, "一次调用只落一行归属索引"
    assert len(emitted) == 1, "一次调用只发一份 trace"


# ──────────────────────────────────────────────── 五、取消能穿透内核


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "hybrid"])
async def test_cancelling_the_call_cancels_the_recall_in_flight(mode: str) -> None:
    """客户端断开 → 内核里正在 await 的嵌入收到取消，后续召回一次都不再发生。"""

    kernel = importlib.import_module("app.services.retrieval.kernel")
    embedding = _BlockingEmbedding()
    vector = _RecordingVectorStore()

    task = asyncio.create_task(
        kernel.retrieve_kernel(
            AsyncRecordingSession(),
            **_kernel_kwargs(
                mode=mode,
                embedding_factory=lambda: embedding,
                vector_factory=lambda: vector,
                keyword_factory=lambda: _RecordingKeyword(),
            ),
        )
    )
    await asyncio.wait_for(embedding.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert embedding.cancelled is True, "取消必须传到正在 await 的那一段"
    assert vector.scopes == [], "取消之后不该再发出召回"


# ───────────────────────────────────────────── 六、注入位：本票只有签名


def test_the_injection_slot_is_a_no_op_default() -> None:
    """注入位默认 ``None`` = 这一步不存在：显式传它一次，结果与不传逐字段相同。"""

    async def go() -> tuple[Any, Any]:
        defaulted, _ = await _run_kernel()
        explicit, _ = await _run_kernel(graph_injection=None)
        return defaulted, explicit

    defaulted, explicit = asyncio.run(go())

    assert defaulted.chunks == explicit.chunks
    assert defaulted.degraded_reason == explicit.degraded_reason
    assert defaulted.rerank == explicit.rerank


# ─────────────────────────────────────────────────── 外层调用的最小夹具


def _options() -> RetrievalOptions:
    return RetrievalOptions(
        mode="hybrid",
        top_k=2,
        fold_to_sections=False,
        expand_table_rows=False,
        rerank=False,
        rewrite=False,
        orchestration=False,
    )


def _install_retrieve_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把组合根的三类 Provider 与可见性换成离线替身（与既有用例同一套接缝）。"""

    service = importlib.import_module("app.services.rag_service")

    embedding = _RecordingEmbedding()
    vector = _RecordingVectorStore()
    keyword = _RecordingKeyword()

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)

    async def _visible(_db: Any, *, tenant_id: str, kb_id: str) -> Any:
        return SimpleNamespace(tenant_id=tenant_id, settings={}, name="", description="")

    monkeypatch.setattr(service, "ensure_visible", _visible)
    return {"embedding": embedding, "vector": vector, "keyword": keyword}
