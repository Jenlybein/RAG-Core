"""第 13 步 · 状态机第 ⑤ 步——Finalizer 接线、四条校验的出口、模型失败即整单失败。

这一层验的是**状态机的第 ⑤ 步**，不是块本身（块的判定在
``tests/test_research_citation_blocks.py`` 里逐条钉住）：

* **Finalizer 只吃最终材料池**：材料池里没有 id、提示词里也没有，模型只能点序号；
* **任一硬校验不过 → ``answer = None``**，而材料、计划、轮次、停止原因照给（ADR-0026 §3）；
* **第 13 步：模型调用失败即整单 50003**（客户重试）。平台不再分强弱模型，四个阶段
  （规划 / 判定 / 复查 / 生成）共用同一个 ``default`` 角色，没有「换模型兜底」这条路；
* **子检索自身的降级原因不被丢掉**：去重后进 ``sub_search_degraded``（一因一码）。

模型是**一份**脚本替身（按阶段顺序交出 规划 / 判定 / 生成 三种输出），检索内核是
记账替身：不发网络、不连库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider
from app.services import research
from app.services.citation_blocks import (
    REJECTED_ASSERTION_WITHOUT_CORE,
    REJECTED_CONFLICT_ADJUDICATED,
    REJECTED_UNRESOLVED_REFERENCE,
)
from app.services.research_limits import MAX_MODEL_CALLS, StopReason

TENANT = "tenant_demo"
QUERY = "退款多久到账，需要哪些材料"


# ─────────────────────────────────────────────────────────────── 离线替身


def _row(key: str, *, content: str | None = None, document: str | None = None) -> dict[str, Any]:
    """一条交付单元（折叠之后的形状：一节一条，带代表片与节身份）。"""

    return {
        "document_id": document or f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "kb_id": "kb-a",
        "title": "退款政策",
        "content": content if content is not None else f"{key} 的正文。",
        "score": 0.9,
        "section_id": f"section-{key}",
        "heading_path": f"退款 > {key}",
    }


class _Search:
    """检索内核替身：按子查询返回预置行，并记下每一次的查询串。"""

    def __init__(self, rows_for: Mapping[str, Sequence[dict[str, Any]]] | None = None) -> None:
        self.rows_for = dict(rows_for or {})
        self.calls: list[str] = []

    async def __call__(self, sub_query: str) -> list[dict[str, Any]]:
        self.calls.append(sub_query)
        return [dict(row) for row in self.rows_for.get(sub_query, ())]


class _Scripted(LLMProvider):
    """脚本替身：按顺序交出预置输出；预置项是异常就当场抛出。"""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if not self._responses:
            raise AssertionError(f"{schema.__name__} 不该被问：脚本用完了")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


def _factory(llm: LLMProvider):
    async def _get() -> LLMProvider:
        return llm

    return _get


def _plan() -> dict:
    return {
        "points": ["退款时限", "所需材料"],
        "queries": [
            {"point": 1, "query": "退款多久到账"},
            {"point": 2, "query": "退款需要哪些材料"},
        ],
    }


def _covered(points: int = 2, *, material: int = 1) -> dict:
    """覆盖判定的预置输出：每个要点都被同一条材料直接支撑（没有缺口）。"""

    return {
        "points": [
            {"point": index, "status": "supported", "materials": [material]}
            for index in range(1, points + 1)
        ],
        "queries": [],
    }


def _blocks(*items: dict) -> dict:
    return {"blocks": list(items)}


def _block(
    text: str,
    *,
    kind: str = "supported",
    primary: tuple[int, ...] = (),
    supporting: tuple[int, ...] = (),
    conflict: bool = False,
) -> dict:
    return {
        "text": text,
        "kind": kind,
        "primary": list(primary),
        "supporting": list(supporting),
        "conflict": conflict,
    }


def _two_rows() -> _Search:
    return _Search({"退款多久到账": [_row("a")], "退款需要哪些材料": [_row("b")]})


def _default_llm(*, blocks: object | None = None, judgement: dict | None = None) -> _Scripted:
    """一份脚本按阶段顺序服务四次模型调用里实际发生的那几次。"""

    return _Scripted(
        _plan(),
        judgement if judgement is not None else _covered(),
        blocks if blocks is not None else _blocks(_block("3 个工作日内到账。", primary=(2,))),
    )


def _run(
    *,
    llm: LLMProvider | None = None,
    search: _Search | None = None,
    sub_search_degraded: Sequence[str] = (),
) -> tuple[research.ResearchOutcome, LLMProvider, _Search]:
    """跑一次完整五步：单一 ``default`` 角色（缺省脚本），四阶段共用一份工厂。"""

    provider = llm if llm is not None else _default_llm()
    probe = search if search is not None else _two_rows()
    outcome = asyncio.run(
        research.run_research(
            query=QUERY,
            tenant_id=TENANT,
            search=probe,
            llm_factory=_factory(provider),
            sub_search_degraded=sub_search_degraded,
        )
    )
    return outcome, provider, probe


# ─────────────────────────────────────────── 正常路径：块 + 渲染串 + 完整度


def test_a_round_produces_citation_blocks_from_the_material_pool() -> None:
    """覆盖全材料 → 每块至少引一条核心片；``answer`` 就是这些块的渲染串。"""

    outcome, provider, _ = _run(
        llm=_default_llm(blocks=_blocks(_block("3 个工作日内到账。", primary=(2,))))
    )

    assert outcome.answer == "3 个工作日内到账。（E2）"
    assert list(outcome.citations) == [
        {
            "text": "3 个工作日内到账。",
            "evidence_ids": ["E2"],
            "status": "supported",
        }
    ]
    # 三个模型阶段各一次：规划 / 判断 / 生成。**没有补证，所以没有 ⑤ 复查**——
    # MAX_MODEL_CALLS 是上限（4），典型路径仍是 3 次。
    assert outcome.model_calls == 3 < MAX_MODEL_CALLS
    assert isinstance(provider, _Scripted)
    assert provider.calls == ["ResearchPlan", "CoverageJudgement", "AnswerOut"]
    assert outcome.evidence_state == "complete"
    assert outcome.stop_reason is StopReason.EVIDENCE_COMPLETE


def test_a_missing_point_becomes_an_insufficient_block_not_invented_text() -> None:
    """材料只盖住一个要点 → 另一块必须是 ``insufficient``（承认不足），完整度是 ``partial``。"""

    (
        outcome,
        _,
        _,
    ) = _run(
        llm=_default_llm(
            judgement={
                "points": [
                    {"point": 1, "status": "supported", "materials": [1]},
                    {"point": 2, "status": "none", "materials": []},
                ],
                "queries": [{"point": 2, "query": "超期退款需要哪些材料"}],
            },
            blocks=_blocks(
                _block("超期退款需人工审核。", primary=(1,)),
                _block("未找到所需材料的说明。", kind="insufficient"),
            ),
        ),
        search=_Search({"退款多久到账": [_row("a")], "退款需要哪些材料": []}),
    )

    assert [citation["status"] for citation in outcome.citations] == [
        "supported",
        "insufficient",
    ]
    assert outcome.answer is not None and "材料不足" in outcome.answer
    assert outcome.evidence_state == "partial"
    assert outcome.gaps == ("A2",)


def test_the_finalizer_prompt_carries_materials_without_internal_ids() -> None:
    """提示词里只有序号、标题与摘录——切片 id / 文档 id / 节 id 一个都不进去。"""

    _, provider, _ = _run()

    assert isinstance(provider, _Scripted)
    prompt = provider.prompts[-1]
    for internal in ("chunk-a", "chunk-b", "doc-a", "section-a"):
        assert internal not in prompt


def test_no_material_means_no_model_call_and_no_answer() -> None:
    """材料池为空 → 不调生成（问了也只能编）：``answer = None``、``evidence_state = missing``。

    判定阶段仍然跑一次（它是覆盖判定的入口），所以脚本替身给两份输出：规划 + 一份
    「所有要点都是 none / 没有支撑」的合法判定（空材料下它没有材料可引用）。
    """

    empty_judgement = {
        "points": [
            {"point": 1, "status": "none", "materials": []},
            {"point": 2, "status": "none", "materials": []},
        ],
        "queries": [],
    }
    outcome, provider, _ = _run(
        llm=_Scripted(_plan(), empty_judgement),
        search=_Search(),
    )

    assert outcome.answer is None
    assert outcome.citations == ()
    assert outcome.evidence_state == "missing"
    assert isinstance(provider, _Scripted)
    assert provider.calls == ["ResearchPlan", "CoverageJudgement"], "没有材料就不该问生成"
    assert outcome.model_calls == 2


# ─────────────────────────────────────────── 四条校验的出口：answer 落成 null


def test_an_assertion_without_a_citation_leaves_the_answer_empty() -> None:
    """有结论却不引证据 → 整份答案作废，材料与计划照给。"""

    (
        outcome,
        _,
        _,
    ) = _run(llm=_default_llm(blocks=_blocks(_block("退款 3 个工作日到账。"))))

    assert outcome.answer is None
    assert outcome.citations == ()
    assert outcome.answer_rejected_reason == REJECTED_ASSERTION_WITHOUT_CORE
    # 材料与计划照给（ADR-0026 §3：不是失败，是仍有产出的返回）。
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]
    assert [point.point_id for point in outcome.points] == ["A1", "A2"]
    assert outcome.stop_reason is StopReason.EVIDENCE_COMPLETE
    assert outcome.evidence_state == "missing"


def test_an_out_of_range_citation_leaves_the_answer_empty() -> None:
    """下标越界 → ``answer = null``（不猜、不截）。"""

    (
        outcome,
        _,
        _,
    ) = _run(llm=_default_llm(blocks=_blocks(_block("3 个工作日内到账。", primary=(9,)))))

    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_UNRESOLVED_REFERENCE
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]


def test_an_internal_identifier_in_the_answer_leaves_it_empty() -> None:
    """答案里漏出切片 id → ``answer = null``。"""

    (
        outcome,
        _,
        _,
    ) = _run(
        llm=_default_llm(
            blocks=_blocks(_block("按 chunk-a 的说法，3 个工作日内到账。", primary=(2,)))
        )
    )

    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_UNRESOLVED_REFERENCE


def test_a_conflict_verdict_leaves_the_answer_empty() -> None:
    """多来源块里替用户裁决 → ``answer = null``（冲突要拆成两块、各自带来源）。"""

    (
        outcome,
        _,
        _,
    ) = _run(
        llm=_default_llm(
            blocks=_blocks(
                _block("以《退款政策》为准：3 个工作日内到账。", primary=(1,), supporting=(2,))
            )
        ),
        search=_Search(
            {
                "退款多久到账": [_row("a", document="doc-a")],
                "退款需要哪些材料": [_row("b", document="doc-b")],
            }
        ),
    )

    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_CONFLICT_ADJUDICATED


# ─────────────────────────── 第 13 步：模型调用失败即整单 50003（无兜底）


@pytest.mark.parametrize("stage_index", [0, 1, 2], ids=["plan", "judge", "finalize"])
def test_a_model_call_failure_at_any_stage_is_a_50003(stage_index: int) -> None:
    """规划 / 判定 / 生成任一阶段的模型调用失败 → 整单 50003，不再静默降级。"""

    responses: list[object] = [
        _plan(),
        _covered(),
        _blocks(_block("3 个工作日内到账。", primary=(2,))),
    ]
    responses[stage_index] = RuntimeError("模型服务 502")
    llm = _Scripted(*responses)

    with pytest.raises(AppError) as caught:
        _run(llm=llm)

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_a_model_call_failure_does_not_produce_a_fallback_marker() -> None:
    """失败就是失败：响应里没有 ``strong_model_fallback`` 这种「其实降级过」的字段。"""

    outcome_fields = set(research.ResearchOutcome.__dataclass_fields__)
    assert "strong_model_fallback" not in outcome_fields


def test_the_model_calls_never_exceed_the_ceiling() -> None:
    """典型路径 3 次（规划 + 判断 + 生成），没有补证与复查时不超过 MAX_MODEL_CALLS。"""

    outcome, _, _ = _run()
    assert outcome.model_calls == 3 <= MAX_MODEL_CALLS


# ─────────────────────────────────────────── 子检索降级：一因一码、去重


def test_degraded_sub_searches_are_collected_once_per_reason() -> None:
    """子检索自身的降级原因去重后进 ``sub_search_degraded``（一因一码，不拼异常文本）。"""

    outcome, _, _ = _run(
        sub_search_degraded=[
            "vector search failed",
            "vector search failed",
            "keyword search failed",
        ]
    )

    assert outcome.sub_search_degraded == ("vector search failed", "keyword search failed")


def test_a_clean_run_has_no_degraded_reason() -> None:
    """正常路径没有降级原因——「没有降级」不能被一个占位串说成有。"""

    outcome, _, _ = _run()

    assert outcome.sub_search_degraded == ()
