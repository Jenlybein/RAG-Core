"""票 #147：切片编排服务本体——候选池、两次结构化调用、按序号回查、E 编号与完整度。

**只测服务的两个输入**（父 spec #138「Testing Decisions」B 行）：候选 + 模型工厂。
不发网络、不连库、不读 ``.env``：模型是脚本替身，候选是折叠之后的交付单元形状的字典。

守的四条安全规则（每条对应票面的一条验收）：

* **只认序号**：模型只会拿到「序号 + 摘录」，也只能交出序号；越界序号整条拒掉，
  不产生任何材料（正文不可能被模型带进来）。
* **正文来自候选**：材料正文逐字等于候选原文，模型返回的文本一律丢弃。
* **完整度由核心片数出来**：模型的自评（多返回的 ``completeness`` 键）不被采信。
* **池子有配额**：≤20 条、给基础召回留 ≥6 个位置、正文合计 ≤12000 字符。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.providers.llm.base import LLMProvider
from app.services import orchestration as orch

KB = "kb-a"
TENANT = "tenant_demo"
SCOPE = {KB: "v1"}


def _row(
    index: int,
    *,
    kb_id: str = KB,
    content: str | None = None,
    injected: bool = False,
    **extra: Any,
) -> dict:
    """一条候选（折叠之后的交付单元形状：一节一条，带代表片 id 与章节路径）。"""

    row: dict[str, Any] = {
        "document_id": f"doc-{index}",
        "chunk_id": f"chunk-{index}",
        "title": f"文档 {index}",
        "content": content if content is not None else f"第 {index} 条的正文。",
        "kb_id": kb_id,
        "score": 1.0 - index / 100,
        "section_id": f"section-{index}",
        "heading_path": f"退款 > 第 {index} 节",
    }
    if injected:
        # #152 的注入项带着自己的出处；#147 先按这个标记把保留位算对。
        row["retrieval_source"] = "graph_injection"
    row.update(extra)
    return row


class _ScriptedLLM(LLMProvider):
    """两次结构化调用的替身：按顺序交出预置输出，并记下每次的 prompt。"""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, str]] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append({"schema": schema.__name__, "system": system, "user": user})
        if not self._responses:
            raise AssertionError("编排只该问两次模型，预置输出已经用完了")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("切编排只走结构化输出")


def _factory(llm: LLMProvider):
    """协程工厂（与 ``get_llm_provider`` 同形）。"""

    async def _get() -> LLMProvider:
        return llm

    return _get


def _run(
    rows: list[dict],
    *,
    points: object = ("退款时限", "所需材料"),
    roles: object = None,
    scope: dict[str, str] | None = None,
    query: str = "退款多久到账",
) -> tuple[orch.OrchestrationOutcome, _ScriptedLLM]:
    """跑一次编排：两次调用的输出由 ``points`` / ``roles`` 预置。"""

    if roles is None:
        roles = {"points": [{"point": 1, "primary": [1]}, {"point": 2, "primary": [2]}]}
    llm = _ScriptedLLM({"points": list(points)} if isinstance(points, tuple) else points, roles)
    outcome = asyncio.run(
        orch.orchestrate_evidence(
            query=query,
            rows=rows,
            tenant_id=TENANT,
            index_versions=dict(scope or SCOPE),
            llm_factory=_factory(llm),
        )
    )
    return outcome, llm


# ─────────────────────────────────────────── 候选池：20 条上限 / 6 个保留位 / 12000 字符


def test_the_pool_never_exceeds_twenty_candidates() -> None:
    """25 条候选进池，只留前 20 条——序号是模型唯一的指针，池子必须封顶。"""

    pool = orch.build_candidates(
        [_row(index) for index in range(1, 26)],
        tenant_id=TENANT,
        index_versions=SCOPE,
    )

    assert len(pool) == orch.MAX_CANDIDATES == 20
    assert [candidate.order for candidate in pool] == list(range(1, 21))
    assert [candidate.chunk_id for candidate in pool][:3] == ["chunk-1", "chunk-2", "chunk-3"]


def test_injected_candidates_cannot_eat_the_six_reserved_slots() -> None:
    """注入项最多占 14 个位置：基础召回的 6 个位置先留下，再谈别的。

    #147 里图谱注入还没落地（#152），所以这条规则今天不会改变任何一次真实结果；
    把它钉在服务里，是为了接线那天不会出现「编排把基础召回挤空」这种回归。
    """

    rows = [_row(index, injected=True) for index in range(1, 21)] + [
        _row(index) for index in range(21, 31)
    ]

    pool = orch.build_candidates(rows, tenant_id=TENANT, index_versions=SCOPE)

    injected = [candidate for candidate in pool if candidate.injected]
    plain = [candidate for candidate in pool if not candidate.injected]
    assert len(pool) <= orch.MAX_CANDIDATES
    assert len(injected) <= orch.MAX_CANDIDATES - orch.MIN_BASE_CANDIDATES
    assert len(plain) >= orch.MIN_BASE_CANDIDATES


def test_the_prompt_budget_stops_at_twelve_thousand_chars() -> None:
    """正文合计 12000 字符是硬上限：超出的候选既不进池，也就不会被模型点到。"""

    rows = [_row(index, content="字" * 3000) for index in range(1, 10)]

    pool = orch.build_candidates(rows, tenant_id=TENANT, index_versions=SCOPE)

    assert sum(len(candidate.content) for candidate in pool) <= orch.MAX_PROMPT_CHARS == 12000
    assert len(pool) == 4, "3000 字一条，12000 字只能装 4 条"
    assert [candidate.order for candidate in pool] == [1, 2, 3, 4]


def test_a_single_section_longer_than_the_budget_is_still_offered_truncated() -> None:
    """单节就超预算时不能整池变空：进池的是被截断的摘录，材料仍取库里的原文。

    截断只发生在提示词这一侧（成本），材料正文一个字都不许截——「逐字一致」是验收。
    """

    rows = [_row(1, content="字" * 20000), _row(2, content="短正文")]

    pool = orch.build_candidates(rows, tenant_id=TENANT, index_versions=SCOPE)

    assert [candidate.order for candidate in pool] == [1]
    prompt = orch.build_points_prompt(query="退款多久到账", candidates=pool)
    assert sum(len(candidate.excerpt) for candidate in pool) <= orch.MAX_PROMPT_CHARS
    assert pool[0].content == "字" * 20000, "材料正文（content）不许被截断"
    assert len(pool[0].excerpt) < len(pool[0].content)
    assert "字" in prompt


def test_candidates_outside_the_request_scope_are_dropped() -> None:
    """逐条校验租户 / 知识库 / 索引版本：越权或跨版本的那一条连序号都不该拿到。

    交付单元本身来自内核，天然在范围内；这一层是给 #152 的注入项准备的——注入项
    可以来自别的库（跨库边允许存在），但**不许**混进本次编排的材料。
    """

    rows = [
        _row(1),
        _row(2, kb_id="kb-other"),
        _row(3, index_version="v9"),
        _row(4, tenant_id="tenant-other"),
        _row(5, chunk_id=""),
    ]

    pool = orch.build_candidates(rows, tenant_id=TENANT, index_versions=SCOPE)

    assert [candidate.chunk_id for candidate in pool] == ["chunk-1"]
    assert pool[0].order == 1, "序号按入池顺序重排，模型只认池子里的位置"


# ─────────────────────────────────────────── 组装：E 编号 / 越界 / 完整度 / 正文来源


def _assemble(points: list[str], roles: list[orch.PointRole], pool: list[orch.Candidate]) -> dict:
    return orch.assemble_block(points=points, roles=roles, candidates=pool)


def test_evidence_ids_follow_point_order_then_primary_before_supporting() -> None:
    """E1…En 按要点顺序稳定分配；同一要点内核心片在辅助片之前。

    稳定可引用是验收之一：调用方会把这串编号写进自己的答复，顺序一变就对不上。
    """

    pool = orch.build_candidates(
        [_row(index) for index in range(1, 4)], tenant_id=TENANT, index_versions=SCOPE
    )

    block = _assemble(
        ["退款时限", "所需材料"],
        [
            orch.PointRole(point=2, primary=(3,), supporting=(2,)),
            orch.PointRole(point=1, primary=(1,), supporting=(2,)),
        ],
        pool,
    )

    assert [point["point_id"] for point in block["points"]] == ["A1", "A2"]
    assert [point["text"] for point in block["points"]] == ["退款时限", "所需材料"]
    assert [item["evidence_id"] for item in block["evidence"]] == ["E1", "E2", "E3", "E4"]
    assert [item["point_id"] for item in block["evidence"]] == ["A1", "A1", "A2", "A2"]
    assert [item["role"] for item in block["evidence"]] == [
        "primary",
        "supporting",
        "primary",
        "supporting",
    ]
    assert [item["chunk_id"] for item in block["evidence"]] == [
        "chunk-1",
        "chunk-2",
        "chunk-3",
        "chunk-2",
    ]


def test_an_out_of_range_index_produces_no_material_at_all() -> None:
    """越界序号整条拒掉：不出材料、不留半条、也不把模型交来的文本当材料。"""

    pool = orch.build_candidates(
        [_row(index) for index in range(1, 3)], tenant_id=TENANT, index_versions=SCOPE
    )

    block = _assemble(
        ["退款时限"],
        [orch.PointRole(point=1, primary=(99, -1, 0), supporting=(3,))],
        pool,
    )

    assert block["evidence"] == [], "越界序号不产生任何材料"
    assert block["points"] == [{"point_id": "A1", "text": "退款时限", "has_primary": False}]
    assert block["completeness"] == "missing"


def test_a_duplicate_index_inside_one_role_is_counted_once() -> None:
    """同一要点里同一个序号点两次：只算一条材料（否则 E 编号会凭空多一节）。"""

    pool = orch.build_candidates(
        [_row(index) for index in range(1, 3)], tenant_id=TENANT, index_versions=SCOPE
    )

    block = _assemble(
        ["退款时限"], [orch.PointRole(point=1, primary=(1, 1), supporting=(1,))], pool
    )

    assert [item["chunk_id"] for item in block["evidence"]] == ["chunk-1", "chunk-1"]
    assert [item["role"] for item in block["evidence"]] == ["primary", "supporting"]
    assert block["completeness"] == "complete"


def test_material_text_comes_from_the_candidate_and_never_from_the_model() -> None:
    """材料正文逐字等于候选原文：模型返回的任何文本都不进响应。"""

    pool = orch.build_candidates(
        [_row(1, content="超期退款需人工审核，3 个工作日内到账。")],
        tenant_id=TENANT,
        index_versions=SCOPE,
    )

    block = _assemble(["退款时限"], [orch.PointRole(point=1, primary=(1,))], pool)

    material = block["evidence"][0]
    assert material["content"] == "超期退款需人工审核，3 个工作日内到账。"
    assert material["chunk_id"] == "chunk-1"
    assert material["kb_id"] == KB
    assert material["document_id"] == "doc-1"
    assert material["heading_path"] == "退款 > 第 1 节"


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (
            [orch.PointRole(point=1, primary=(1,)), orch.PointRole(point=2, primary=(2,))],
            "complete",
        ),
        (
            [orch.PointRole(point=1, primary=(1,)), orch.PointRole(point=2, supporting=(2,))],
            "partial",
        ),
        (
            [orch.PointRole(point=1, supporting=(1,)), orch.PointRole(point=2)],
            "missing",
        ),
    ],
)
def test_completeness_is_counted_from_primary_materials(
    roles: list[orch.PointRole], expected: str
) -> None:
    """三态完整度由「有没有核心片」数出来：全有 = complete，部分 = partial，都没有 = missing。"""

    pool = orch.build_candidates(
        [_row(index) for index in range(1, 3)], tenant_id=TENANT, index_versions=SCOPE
    )

    block = _assemble(["退款时限", "所需材料"], roles, pool)

    assert block["completeness"] == expected
    assert [point["has_primary"] for point in block["points"]] == [
        any(
            item["point_id"] == point["point_id"] and item["role"] == "primary"
            for item in block["evidence"]
        )
        for point in block["points"]
    ]


def test_a_point_without_any_material_is_still_listed() -> None:
    """没有材料的要点照样列出来：调用方要知道「缺哪一块」，而不是只拿到有料的那几个。"""

    pool = orch.build_candidates([_row(1)], tenant_id=TENANT, index_versions=SCOPE)

    block = _assemble(["退款时限", "发票怎么补"], [orch.PointRole(point=1, primary=(1,))], pool)

    assert [point["text"] for point in block["points"]] == ["退款时限", "发票怎么补"]
    assert [point["has_primary"] for point in block["points"]] == [True, False]
    assert block["completeness"] == "partial"


# ─────────────────────────────────────────── 两次结构化调用


def test_the_model_is_asked_twice_points_then_roles() -> None:
    """先要要点、再要角色：两次都是结构化输出，模型两次都只拿到序号与摘录。"""

    outcome, llm = _run([_row(index) for index in range(1, 4)])

    assert outcome.degraded_reason is None
    assert [call["schema"] for call in llm.calls] == ["PointList", "PointRoles"]
    for call in llm.calls:
        prompt = call["user"]
        assert "1." in prompt and "2." in prompt and "3." in prompt
        assert "chunk-1" not in prompt, "切片 id 不进提示词：模型没有可回显的身份"
        assert "doc-1" not in prompt


def test_the_roles_prompt_carries_the_points_as_stable_labels() -> None:
    """第二次调用拿得到 A1…An：角色的归属只能靠这两个稳定标记对上。"""

    outcome, llm = _run([_row(1)], points=("退款时限", "到账凭证"))

    assert outcome.block is not None
    roles_prompt = llm.calls[1]["user"]
    assert "A1" in roles_prompt and "A2" in roles_prompt
    assert "退款时限" in roles_prompt and "到账凭证" in roles_prompt


def test_the_block_shape_is_points_evidence_and_completeness() -> None:
    """整块形状：``completeness`` + ``points`` + ``evidence``，够调用方直接渲染。"""

    outcome, _ = _run(
        [_row(1), _row(2)],
        points=("退款时限",),
        roles={"points": [{"point": 1, "primary": [1], "supporting": [2]}]},
    )

    assert outcome.block is not None
    assert set(outcome.block) == {"completeness", "points", "evidence"}
    assert outcome.block["completeness"] == "complete"
    assert outcome.block["points"] == [{"point_id": "A1", "text": "退款时限", "has_primary": True}]
    assert [item["evidence_id"] for item in outcome.block["evidence"]] == ["E1", "E2"]
