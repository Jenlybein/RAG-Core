"""#144 · 候选注入 + 折叠关闭时的父节恢复（ADR-0027 决策 4 / 5 / 6 / 7）。

三条接缝，各测各的：

| 接缝 | 测什么 | 不测什么 |
| --- | --- | --- |
| ``graph_injection.inject_candidates``（吃替身图谱） | 优先级、三条上限、``score = 0``、来源标记、范围核验、失败降级 | 不测 SQL 长什么样 |
| ``build_outbound_statement`` / ``build_representative_statement``（纯函数） | 范围条件与「路标片不参战」编译出来是什么形状 | 不跑真库 |
| ``retrieve_kernel`` / ``collapse_results`` | 三种检索方式都吃到注入、注入在重排之前、父节恢复不改排名 | 不打断言内核内部 |

数据库一律离线：图谱读模型用替身（一个开关替身掉整个图谱实现，见 ADR-0027 的测试接缝）。
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.repositories import chunk_repository, section_edge_repository
from app.services import orchestration
from app.services.retrieval_options import RetrievalOptions
from app.services.graph_injection import (
    INJECTED_SOURCE,
    MAX_INJECTED,
    MAX_SEEDS,
    NEIGHBOURS_PER_SEED,
    REASON_GRAPH_FAILED,
    InjectedSection,
    InjectionOutcome,
    SeedSection,
    inject_candidates,
    select_candidates,
)
from tests.fake_db import AsyncRecordingSession

TENANT = "tenant_graph"
KB_A = "kb-a"
KB_B = "kb-b"
VERSION = DEFAULT_INDEX_VERSION


# ─────────────────────────────────────────────────────────────── 离线替身


class _FakeGraph:
    """图谱读模型的替身：种子与邻居都预置，失败按需抛。"""

    def __init__(
        self,
        *,
        seeds: Sequence[SeedSection] | None = None,
        neighbours: Sequence[InjectedSection] | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self._seed_script = list(seeds or [])
        self._neighbour_script = list(neighbours or [])
        self.failure = failure
        self.seed_calls: list[list[dict]] = []
        self.neighbour_calls: list[list[SeedSection]] = []

    async def seed_sections(
        self,
        *,
        tenant_id: str,
        index_versions: Mapping[str, str],
        chunks: Sequence[Mapping[str, Any]],
    ) -> list[SeedSection]:
        if self.failure is not None:
            raise self.failure
        self.seed_calls.append([dict(chunk) for chunk in chunks])
        return list(self._seed_script)

    async def neighbours(
        self,
        *,
        tenant_id: str,
        index_versions: Mapping[str, str],
        seeds: Sequence[SeedSection],
    ) -> list[InjectedSection]:
        if self.failure is not None:
            raise self.failure
        self.neighbour_calls.append(list(seeds))
        return list(self._neighbour_script)


def _hit(
    chunk_id: str,
    *,
    kb_id: str = KB_A,
    document_id: str = "doc-1",
    score: float = 0.9,
    section_id: str | None = None,
) -> dict:
    """一条召回命中（融合之后、注入之前的池子里的形状）。"""

    row: dict[str, Any] = {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "title": "退款政策",
        "content": f"正文-{chunk_id}",
        "kb_id": kb_id,
        "score": score,
    }
    if section_id is not None:
        row["section_id"] = section_id
    return row


def _neighbour(
    section_id: str,
    *,
    seed_section_id: str = "sec-2.3",
    relation_type: str = "sibling",
    kb_id: str = KB_A,
    index_version: str = VERSION,
    document_id: str = "doc-1",
    chunk_id: str | None = None,
    content: str | None = None,
) -> InjectedSection:
    return InjectedSection(
        kb_id=kb_id,
        index_version=index_version,
        document_id=document_id,
        section_id=section_id,
        chunk_id=chunk_id or f"chunk-{section_id}",
        content=content if content is not None else f"正文-{section_id}",
        title="退款政策",
        heading_path=f"退款 > {section_id}",
        relation_type=relation_type,
        seed_section_id=seed_section_id,
    )


def _seed(
    section_id: str = "sec-2.3", *, kb_id: str = KB_A, document_id: str = "doc-1"
) -> SeedSection:
    return SeedSection(kb_id=kb_id, document_id=document_id, section_id=section_id)


async def _inject(
    *,
    graph: _FakeGraph,
    chunks: Sequence[dict],
    index_versions: Mapping[str, str] | None = None,
) -> InjectionOutcome:
    return await inject_candidates(
        graph=graph,
        tenant_id=TENANT,
        index_versions=index_versions if index_versions is not None else {KB_A: VERSION},
        chunks=list(chunks),
    )


# ─────────────────────────────────────────── 一、候选进来，但分数是 0


@pytest.mark.asyncio
async def test_hitting_2_3_also_brings_2_4_into_the_pool_with_score_zero() -> None:
    """命中 2.3 时 2.4 进候选池，且注入项 ``score`` 为 0（是否入选交给重排）。"""

    graph = _FakeGraph(seeds=[_seed("sec-2.3")], neighbours=[_neighbour("sec-2.4")])

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")])

    assert [row["chunk_id"] for row in outcome.chunks] == ["c-2.3", "chunk-sec-2.4"]
    injected = outcome.chunks[-1]
    assert injected["score"] == 0.0, "图谱不裁决相关性：注入项的分数必须是 0，不是一个高分"
    assert injected["content"] == "正文-sec-2.4", "注入的是目标节的一个代表片"
    assert injected["section_id"] == "sec-2.4"
    assert outcome.injected == 1
    assert outcome.degraded_reason is None


@pytest.mark.asyncio
async def test_only_injected_rows_carry_the_source_marker() -> None:
    """直接命中项**不带**来源键：调用方据此区分「直接命中」与「按关系补进来」。"""

    graph = _FakeGraph(seeds=[_seed("sec-2.3")], neighbours=[_neighbour("sec-2.4")])

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")])

    assert outcome.chunks[0].get("retrieval_source") is None
    assert outcome.chunks[-1]["retrieval_source"] == INJECTED_SOURCE == "graph_injection"


@pytest.mark.asyncio
async def test_the_injected_row_is_shaped_like_a_recall_row() -> None:
    """注入项与召回行同形：五个分列字段是 ``None`` 而不是 0（「没命中」≠「0 分」）。"""

    graph = _FakeGraph(seeds=[_seed("sec-2.3")], neighbours=[_neighbour("sec-2.4")])

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")])

    injected = outcome.chunks[-1]
    for column in ("vector_score", "bm25_score", "vector_rank", "bm25_rank", "rrf_score"):
        assert injected[column] is None, f"{column} 必须是 None：注入项没有在任何一路召回里得分"


@pytest.mark.asyncio
async def test_injected_rows_never_eat_the_six_reserved_slots() -> None:
    """注入项再多，也不能把基础召回的 6 个保留位吃掉（#147 的池子按来源标记算位）。"""

    seeds = [_seed(f"sec-{index}") for index in range(10)]
    neighbours = [
        _neighbour(f"target-{index}", seed_section_id=f"sec-{index}") for index in range(10)
    ]
    base = [_hit(f"c-{index}", section_id=f"sec-{index}") for index in range(6)]
    graph = _FakeGraph(seeds=seeds, neighbours=neighbours)

    outcome = await _inject(graph=graph, chunks=base)
    pool = orchestration.build_candidates(
        outcome.chunks, tenant_id=TENANT, index_versions={KB_A: VERSION}
    )

    kept_base = [candidate for candidate in pool if not candidate.injected]
    assert len(kept_base) >= orchestration.MIN_BASE_CANDIDATES, (
        "六条基础召回必须都在池子里：保留位先给它们"
    )
    injected = [candidate for candidate in pool if candidate.injected]
    assert len(injected) <= orchestration.MAX_CANDIDATES - orchestration.MIN_BASE_CANDIDATES


# ─────────────────────────────────────────── 二、优先级与三条上限


@pytest.mark.asyncio
async def test_reference_beats_sibling_beats_parent() -> None:
    """一个种子三个邻居、每个种子只留两条 → 留下的是引用与兄弟节，父节被挤掉。"""

    graph = _FakeGraph(
        seeds=[_seed("sec-2.3")],
        neighbours=[
            _neighbour("sec-parent", relation_type="parent"),
            _neighbour("sec-2.4", relation_type="sibling"),
            _neighbour("sec-ref", relation_type="reference"),
        ],
    )

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")])

    injected = [row["section_id"] for row in outcome.chunks[1:]]
    assert injected == ["sec-ref", "sec-2.4"]
    assert outcome.by_relation == {"reference": 1, "sibling": 1}


@pytest.mark.asyncio
async def test_each_seed_gives_at_most_two_neighbours() -> None:
    """一个种子给四条邻居也只留两条：一条种子十条邻居等于用图谱把池子重填一遍。"""

    graph = _FakeGraph(
        seeds=[_seed("sec-2.3")],
        neighbours=[_neighbour(f"sec-{index}") for index in range(4)],
    )

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")])

    assert outcome.injected == NEIGHBOURS_PER_SEED == 2


@pytest.mark.asyncio
async def test_at_most_ten_seeds_are_used() -> None:
    """种子只取融合结果的前 10 条：尾部整体是弱命中，扩出来的是噪声。"""

    graph = _FakeGraph(seeds=[], neighbours=[])

    await _inject(graph=graph, chunks=[_hit(f"c-{index}") for index in range(15)])

    assert len(graph.seed_calls) == 1
    assert len(graph.seed_calls[0]) == MAX_SEEDS == 10
    assert [chunk["chunk_id"] for chunk in graph.seed_calls[0]] == [
        f"c-{index}" for index in range(10)
    ]


@pytest.mark.asyncio
async def test_the_injected_pool_is_capped_at_thirty() -> None:
    """20 个种子 × 2 条 = 40 条候选，注入项仍只留 30 条（重排有成本）。"""

    seeds = [_seed(f"sec-{index}") for index in range(20)]
    neighbours = [
        _neighbour(f"target-{index}-{side}", seed_section_id=f"sec-{index}")
        for index in range(20)
        for side in range(2)
    ]
    graph = _FakeGraph(seeds=seeds, neighbours=neighbours)

    outcome = await _inject(graph=graph, chunks=[_hit(f"c-{index}") for index in range(20)])

    assert outcome.injected == MAX_INJECTED == 30


def test_the_caps_are_constants_not_settings() -> None:
    """三条上限是常量：可调旋钮在样本量不足时必然被调成过拟合值（与折叠 over-fetch 同一条理由）。"""

    module = importlib.import_module("app.services.graph_injection")

    assert (module.MAX_SEEDS, module.NEIGHBOURS_PER_SEED, module.MAX_INJECTED) == (10, 2, 30)
    assert not hasattr(module, "settings"), (
        "上限不许跟着部署配置漂：那会让同一次评测在两台机器上不可比"
    )


@pytest.mark.asyncio
async def test_a_section_already_in_the_pool_is_not_injected_twice() -> None:
    """池子里已有的节不重复注入：既不加信息，还白占一个重排名额。"""

    graph = _FakeGraph(
        seeds=[_seed("sec-2.3")],
        neighbours=[
            _neighbour("sec-2.3", chunk_id="c-2.3"),  # 就是池子里那条
            _neighbour("sec-2.4", chunk_id="c-2.4"),  # 代表片已在池子里
            _neighbour("sec-2.5"),
        ],
    )

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3"), _hit("c-2.4")])

    assert outcome.injected == 1
    assert outcome.chunks[-1]["section_id"] == "sec-2.5"


# ─────────────────────────────────────────── 三、范围：目标节必须在本次请求里


@pytest.mark.asyncio
async def test_a_target_in_another_kb_is_dropped() -> None:
    """请求只带 A 库时，指向 B 库的邻居不生效（越权由范围堵死）。"""

    graph = _FakeGraph(
        seeds=[_seed("sec-2.3")],
        neighbours=[_neighbour("sec-b", kb_id=KB_B)],
    )

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")], index_versions={KB_A: VERSION})

    assert outcome.injected == 0
    assert [row["chunk_id"] for row in outcome.chunks] == ["c-2.3"]
    assert outcome.degraded_reason is None, "范围外的边是「不该注入」，不是故障"


@pytest.mark.asyncio
async def test_a_target_from_another_index_version_is_dropped() -> None:
    """版本对不上同样不生效：旧边指向新版本的节是「不报错的错答案」（ADR-0025）。"""

    graph = _FakeGraph(
        seeds=[_seed("sec-2.3")],
        neighbours=[_neighbour("sec-2.4", index_version="v2")],
    )

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")], index_versions={KB_A: VERSION})

    assert outcome.injected == 0


def _compiled(statement: Any) -> str:
    return " ".join(
        str(
            statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        ).split()
    )


def test_the_neighbour_query_is_scoped_by_the_requested_kb_and_version() -> None:
    """邻居查询的范围条件：请求只带 A 库时，SQL 里不出现 B 库，且是「库 + 版本」的配对。

    反例守卫：写成 ``kb_id IN (...)`` 配 ``index_version IN (...)`` 会匹配出「A 库配
    B 库版本」这种不存在的组合——那是越权，而且不报错。
    """

    sql = _compiled(
        section_edge_repository.build_outbound_statement(
            tenant_id=TENANT,
            index_versions={KB_A: VERSION},
            seeds=[(KB_A, "doc-1", "sec-2.3")],
        )
    )

    assert "section_edges.tenant_id = 'tenant_graph'" in sql, "租客过滤是强制条件"
    assert f"section_edges.kb_id = '{KB_A}'" in sql
    assert f"section_edges.index_version = '{VERSION}'" in sql
    assert KB_B not in sql, "请求没带 B 库，B 库的边一条都不许出现在条件里"
    assert (
        " IN (" in sql
        and "(section_edges.source_document_id, section_edges.source_section_id) IN" in sql
    ), "种子用行值 IN：两段身份一起对，单段就对不出「哪个文档的哪一节」"


def test_the_neighbour_query_is_exactly_one_hop() -> None:
    """只有一跳：不做多跳图遍历（父 spec「Out of Scope」）。"""

    sql = _compiled(
        section_edge_repository.build_outbound_statement(
            tenant_id=TENANT,
            index_versions={KB_A: VERSION, KB_B: "v2"},
            seeds=[(KB_A, "doc-1", "sec-2.3")],
        )
    )

    assert sql.upper().count("FROM SECTION_EDGES") == 1, "自连接就是第二跳"
    assert " JOIN " not in sql.upper()
    # 两对「库 + 版本」之间是 OR：配对关系只能从映射里读出来。
    assert sql.count(" OR ") >= 1
    assert " IN (" not in sql.split("WHERE")[0], "范围条件不是独立的 IN"


def test_the_representative_query_skips_pointer_chunks_and_pins_the_scope() -> None:
    """代表片查询：同一节取第一片，路标片不参战，范围与召回同一条配对条件。"""

    sql = _compiled(
        chunk_repository.build_representative_statement(
            tenant_id=TENANT,
            index_versions={KB_A: VERSION},
            targets=[(KB_A, "doc-1", "sec-2.4")],
        )
    )

    assert "chunks.tenant_id = 'tenant_graph'" in sql
    assert f"chunks.kb_id = '{KB_A}' AND chunks.index_version = '{VERSION}'" in sql
    assert "reference_pointer" in sql, "路标切片只用于建边、不参与召回（也不参与注入）"
    assert "ORDER BY" in sql and "chunk_index" in sql, "同一节取哪一片必须确定"


class _RepresentativeSession:
    """只回答「取代表片」这一条查询的会话替身（按 SQL 行序返回预置行）。"""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> "_RepresentativeSession":
        self.statements.append(statement)
        return self

    def all(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


@pytest.mark.asyncio
async def test_each_section_contributes_exactly_one_representative() -> None:
    """同一节的多片只取排序后的第一片：注入的是代表片，不是整节。"""

    session = _RepresentativeSession(
        [
            (KB_A, "doc-1", "sec-2.4", "chunk-first", "第一片", "退款政策", "退款 > 2.4"),
            (KB_A, "doc-1", "sec-2.4", "chunk-second", "第二片", "退款政策", "退款 > 2.4"),
        ]
    )

    representatives = await chunk_repository.fetch_section_representatives(
        session,  # type: ignore[arg-type]
        tenant_id=TENANT,
        index_versions={KB_A: VERSION},
        targets=[(KB_A, "doc-1", "sec-2.4")],
    )

    assert representatives == {
        (KB_A, "doc-1", "sec-2.4"): ("chunk-first", "第一片", "退款政策", "退款 > 2.4")
    }


# ─────────────────────────────────────────── 四、失败只降级，不抛、不拼异常文本


@pytest.mark.asyncio
async def test_a_failed_relation_query_keeps_the_main_path_and_records_one_code() -> None:
    """关系查询失败 → 跳过注入、主链路照跑、一条专码（不拼异常文本）。"""

    graph = _FakeGraph(failure=RuntimeError("connection reset by peer"))

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3"), _hit("c-2.4")])

    assert [row["chunk_id"] for row in outcome.chunks] == ["c-2.3", "c-2.4"]
    assert outcome.injected == 0
    assert outcome.degraded_reason == REASON_GRAPH_FAILED == "graph injection failed"
    assert "connection" not in outcome.degraded_reason
    assert "RuntimeError" not in outcome.degraded_reason


@pytest.mark.asyncio
async def test_a_degraded_graph_exposes_no_counts_to_metadata() -> None:
    """降级时不能凭空报一个注入条数：``as_metadata`` 里的 0 是「真的没有」，不是占位。"""

    graph = _FakeGraph(failure=RuntimeError("boom"))

    outcome = await _inject(graph=graph, chunks=[_hit("c-2.3")])

    assert outcome.as_metadata() == {"injected": 0, "seeds": 0, "by_relation": {}}


@pytest.mark.asyncio
async def test_hits_without_a_section_are_not_a_failure() -> None:
    """无标题文档的命中没有节身份：这一步无从下手，但那不是故障。"""

    graph = _FakeGraph(seeds=[])

    outcome = await _inject(graph=graph, chunks=[_hit("c-1")])

    assert outcome.injected == 0
    assert outcome.degraded_reason is None
    assert graph.neighbour_calls == [], "没有种子就不该发第二次查询"


@pytest.mark.asyncio
async def test_an_empty_pool_never_touches_the_graph() -> None:
    """空候选池连播种都不做：一次查询也不发。"""

    graph = _FakeGraph(seeds=[_seed()])

    outcome = await _inject(graph=graph, chunks=[])

    assert outcome.chunks == []
    assert graph.seed_calls == []


# ─────────────────────────────────────────── 五、纯策略：上限与去重一起生效


def test_selector_applies_the_caps_without_any_database() -> None:
    """纯函数直接验三条上限：种子顺序决定注入顺序，每种子两条，总数封顶。"""

    seeds = [_seed(f"sec-{index}") for index in range(3)]
    neighbours = [
        _neighbour(f"target-{index}-{side}", seed_section_id=f"sec-{index}")
        for index in range(3)
        for side in range(3)
    ]

    selected = select_candidates(
        seeds=seeds,
        neighbours=neighbours,
        taken_sections=[],
        taken_chunk_ids=[],
    )

    assert [item.section_id for item in selected] == [
        "target-0-0",
        "target-0-1",
        "target-1-0",
        "target-1-1",
        "target-2-0",
        "target-2-1",
    ]


# ─────────────────────────────────── 六、内核接缝：三种检索方式都吃到图谱


class _RecordingEmbedding:
    async def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _RecordingVectorStore:
    async def similarity_search_scope(
        self, db: Any, query_vector: Any, *, tenant_id: str, index_versions: Any, top_k: int
    ) -> list[dict]:
        kb_id = next(iter(index_versions))
        return [dict(_hit("c-2.3"), kb_id=kb_id)]


class _RecordingKeyword:
    available = True

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: Any, top_k: int
    ) -> list[dict]:
        return [dict(_hit("c-2.3", score=9.0))]


class _RecordingRerank:
    """重排替身：记下「它看到了哪些候选」——注入到底在重排之前还是之后，由此可验。"""

    available = True
    provider_name = "stub"
    model_name = None

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        self.seen.append([dict(chunk) for chunk in chunks])
        return list(chunks)[:top_n]


def _injected_row(section_id: str = "sec-2.4", *, chunk_id: str | None = None) -> dict:
    """一条注入项的**形状**（与 ``graph_injection`` 产出的一模一样：0 分 + 来源标记）。"""

    return {
        "document_id": "doc-1",
        "chunk_id": chunk_id or f"chunk-{section_id}",
        "title": "退款政策",
        "content": f"正文-{section_id}",
        "kb_id": KB_A,
        "score": 0.0,
        "section_id": section_id,
        "heading_path": f"退款 > {section_id}",
        "retrieval_source": INJECTED_SOURCE,
        "vector_score": None,
        "bm25_score": None,
        "vector_rank": None,
        "bm25_rank": None,
        "rrf_score": None,
    }


def _graph_slot(
    calls: list[tuple[list[dict], dict[str, str]]],
    *,
    rows: Sequence[dict] | None = None,
    reason: str | None = None,
) -> Any:
    """注入位的替身：记下「拿到什么池子、什么范围」，按预置结果交回。"""

    injected = list(rows if rows is not None else [_injected_row()])

    async def _inject(chunks: list[dict], index_versions: Mapping[str, str]) -> InjectionOutcome:
        calls.append(([dict(chunk) for chunk in chunks], dict(index_versions)))
        return InjectionOutcome(
            chunks=[*chunks, *injected],
            injected=len(injected),
            seeds=1,
            by_relation={"sibling": len(injected)},
            degraded_reason=reason,
        )

    return _inject


async def _run_kernel(
    *,
    mode: str,
    graph_injection: Any,
    rerank: _RecordingRerank | None = None,
    fold_to_sections: bool = False,
    rerank_enabled: bool = False,
) -> Any:
    """直接调内核一次：三种模式共用同一份入参，只有 ``mode`` 不同。"""

    kernel = importlib.import_module("app.services.retrieval.kernel")
    return await kernel.retrieve_kernel(
        AsyncRecordingSession(),
        tenant_id=TENANT,
        index_versions={KB_A: VERSION},
        query="超期多久到账",
        mode=mode,
        vector_queries=("超期多久到账",),
        rerank_query="超期多久到账",
        keyword_query="超期多久到账",
        top_k=4,
        recall_top_k=4,
        rerank_enabled=rerank_enabled,
        rerank_candidates=8 if rerank_enabled else None,
        rerank_factory=(lambda: _async(rerank)) if rerank is not None else None,
        fold_to_sections=fold_to_sections,
        expand_table_rows=False,
        embedding_factory=_RecordingEmbedding,
        vector_factory=_RecordingVectorStore,
        keyword_factory=_RecordingKeyword,
        graph_injection=graph_injection,
    )


async def _async(value: Any) -> Any:
    """协程工厂的替身外壳（注入缝与生产工厂同形，#137）。"""

    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
async def test_injection_lands_in_the_pool_in_all_three_modes(mode: str) -> None:
    """向量 / 关键词 / 混合三种检索方式都要吃到图谱（单路评测也能验证它）。"""

    calls: list[tuple[list[dict], dict[str, str]]] = []

    outcome = await _run_kernel(mode=mode, graph_injection=_graph_slot(calls))

    assert len(calls) == 1, "注入位在「候选池成形之后」，三种模式各经过它一次"
    assert [row["chunk_id"] for row in outcome.chunks] == ["c-2.3", "chunk-sec-2.4"]
    injected = outcome.chunks[-1]
    assert injected["score"] == 0.0
    assert injected["retrieval_source"] == INJECTED_SOURCE


@pytest.mark.asyncio
async def test_hybrid_hands_the_injected_rows_to_the_reranker() -> None:
    """注入发生在**重排之前**：重排看到的是含注入项的池子，名次因此由它裁决。"""

    calls: list[tuple[list[dict], dict[str, str]]] = []
    rerank = _RecordingRerank()

    await _run_kernel(
        mode="hybrid",
        graph_injection=_graph_slot(calls),
        rerank=rerank,
        rerank_enabled=True,
    )

    assert len(rerank.seen) == 1
    assert "chunk-sec-2.4" in [row["chunk_id"] for row in rerank.seen[0]], (
        "注入项必须进到重排的输入里，否则「是否入选交给重排」只是句话"
    )
    assert [row["chunk_id"] for row in calls[0][0]] == ["c-2.3"], "注入位拿到的是融合结果"


@pytest.mark.asyncio
async def test_the_injection_slot_receives_the_requested_scope() -> None:
    """注入位拿到的范围是**本次请求**那一份映射（不是重建一次，也不是空）。"""

    calls: list[tuple[list[dict], dict[str, str]]] = []

    await _run_kernel(mode="vector", graph_injection=_graph_slot(calls))

    assert calls[0][1] == {KB_A: VERSION}


@pytest.mark.asyncio
async def test_a_kernel_without_the_slot_is_byte_identical() -> None:
    """不传注入位 = 这一步不存在：结果与传一个「什么都没注入」的位逐字段相同。"""

    untouched = await _run_kernel(mode="hybrid", graph_injection=None)
    empty = await _run_kernel(mode="hybrid", graph_injection=_graph_slot([], rows=[], reason=None))

    assert untouched.chunks == empty.chunks
    assert untouched.degraded_reason == empty.degraded_reason
    assert untouched.rerank == empty.rerank
    assert untouched.graph is None and empty.graph is not None


# ──────────────────────────────── 七、组合根：开关、观测栏与「先到的才是根因」


class _UnavailableKeyword:
    """关键词引擎没配：混合模式下这一路失败 → 通道故障原因先到。"""

    available = False

    async def keyword_search(self, *args: Any, **kwargs: Any) -> list[dict]:  # pragma: no cover
        raise AssertionError("不可用的引擎不该被调用")


def _options(*, mode: str = "hybrid") -> RetrievalOptions:
    return RetrievalOptions(
        mode=mode,
        top_k=4,
        fold_to_sections=False,
        expand_table_rows=False,
        rerank=False,
        rewrite=False,
        orchestration=False,
    )


def _install_entry_stubs(
    monkeypatch: pytest.MonkeyPatch, *, keyword_available: bool = True
) -> None:
    """把组合根的 Provider 与两个观测旁路换成离线替身（与既有用例同一套接缝）。"""

    service = importlib.import_module("app.services.rag_service")

    keyword = _RecordingKeyword() if keyword_available else _UnavailableKeyword()
    monkeypatch.setattr(service, "get_embedding_provider", _RecordingEmbedding)
    monkeypatch.setattr(service, "get_vector_store", _RecordingVectorStore)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    async def _visible(_db: Any, *, tenant_id: str, kb_id: str) -> Any:
        return SimpleNamespace(
            id=kb_id,
            tenant_id=tenant_id,
            settings={},
            name="",
            description="",
            active_index_version=DEFAULT_INDEX_VERSION,
        )

    async def _no_record(**kwargs: Any) -> None:
        return None

    async def _no_emit(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(service, "ensure_visible", _visible)
    monkeypatch.setattr(service, "record_retrieval_trace", _no_record)
    monkeypatch.setattr(service, "emit_retrieval_trace_off_loop", _no_emit)


def _graph_factory(
    *, neighbours: Sequence[InjectedSection] | None = None, failure: BaseException | None = None
) -> tuple[Any, _FakeGraph]:
    """协程工厂（与 ``get_rerank_provider`` 那条注入缝同形）。"""

    graph = _FakeGraph(
        seeds=[_seed("sec-2.3")],
        neighbours=list(neighbours if neighbours is not None else [_neighbour("sec-2.4")]),
        failure=failure,
    )

    async def _factory() -> Any:
        return graph

    return _factory, graph


async def _retrieve(
    *,
    graph_factory: Any = None,
    mode: str = "hybrid",
) -> dict:
    service = importlib.import_module("app.services.rag_service")
    return await service.retrieve(
        AsyncRecordingSession(),
        tenant_id=TENANT,
        kb_ids=[KB_A],
        query="超期多久到账",
        options=_options(mode=mode),
        graph_factory=graph_factory,
    )


@pytest.mark.asyncio
async def test_the_entry_reports_the_injected_counts_in_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """观测走既有那一栏：``metadata.retrieval.graph`` 给条数与来源，不新造一套口径。"""

    _install_entry_stubs(monkeypatch)
    factory, _ = _graph_factory()

    payload = await _retrieve(graph_factory=factory)

    assert payload["metadata"]["retrieval"]["graph"] == {
        "injected": 1,
        "seeds": 1,
        "by_relation": {"sibling": 1},
    }
    injected = payload["retrieved_chunks"][-1]
    assert injected["score"] == 0.0
    assert injected["retrieval_source"] == INJECTED_SOURCE
    assert payload["retrieved_chunks"][0].get("retrieval_source") is None


@pytest.mark.asyncio
async def test_without_a_graph_factory_nothing_new_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """不开图谱：metadata 里没有 graph 那一栏，结果里也没有来源标记（关闭时逐字节不变）。"""

    _install_entry_stubs(monkeypatch)

    payload = await _retrieve(graph_factory=None)

    assert "graph" not in payload["metadata"]["retrieval"]
    assert all("retrieval_source" not in row for row in payload["retrieved_chunks"])
    assert payload["metadata"]["retrieval"]["degraded"] is False


@pytest.mark.asyncio
async def test_a_graph_failure_degrades_the_call_but_keeps_the_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关系查询失败：检索照常返回、只多一条专码降级原因。"""

    _install_entry_stubs(monkeypatch)
    factory, _ = _graph_factory(failure=RuntimeError("relation table is gone"))

    payload = await _retrieve(graph_factory=factory)

    retrieval = payload["metadata"]["retrieval"]
    assert payload["retrieved_chunks"], "主链路结果照旧返回"
    assert retrieval["degraded"] is True
    assert retrieval["degraded_reason"] == REASON_GRAPH_FAILED
    assert "relation" not in retrieval["degraded_reason"], "不拼异常文本（一因一码）"
    assert retrieval["graph"]["injected"] == 0


@pytest.mark.asyncio
async def test_an_earlier_channel_failure_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """先到的才是根因：关键词一路先挂、图谱后挂，原因仍是关键词那一条。"""

    _install_entry_stubs(monkeypatch, keyword_available=False)
    factory, _ = _graph_factory(failure=RuntimeError("boom"))

    payload = await _retrieve(graph_factory=factory)

    retrieval = payload["metadata"]["retrieval"]
    assert retrieval["degraded_reason"] == "keyword search failed"
    assert retrieval["degraded_reason"] != REASON_GRAPH_FAILED
