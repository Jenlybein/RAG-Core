"""票 #149：状态机第 ⑤ 步——Finalizer 接线、四条校验的出口、强模型兜底与降级观察点。

这一层验的是**状态机的第 ⑤ 步**，不是块本身（块的判定在
``tests/test_research_citation_blocks.py`` 里逐条钉住）：

* **Finalizer 只吃最终材料池**：材料池里没有 id、提示词里也没有，模型只能点序号；
* **任一硬校验不过 → ``answer = None``**，而材料、计划、轮次、停止原因照给（ADR-0026 §3）；
* **强模型未配置 / 失败 / 超时 → 快模型兜底**，并在 ``strong_model_fallback`` 标一个固定串，
  **不报错**；换了兜底之后模型调用次数仍不越过含兜底位的上限；
* **子检索自身的降级原因不被丢掉**：去重后进 ``sub_search_degraded``（一因一码的固定串）。

模型是三份脚本替身（规划 / 判断 / 生成），检索内核是记账替身：不发网络、不连库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.providers.llm.base import LLMProvider
from app.services import research
from app.services.citation_blocks import (
    REJECTED_ASSERTION_WITHOUT_CORE,
    REJECTED_CONFLICT_ADJUDICATED,
    REJECTED_UNRESOLVED_REFERENCE,
)
from app.services.research_limits import MAX_MODEL_CALLS, MAX_MODEL_CALLS_WITH_FALLBACK, StopReason

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


class _Unavailable(LLMProvider):
    """未配置强模型时工厂交回的空实现：``available`` 为假，且**一问就炸**（证明没问）。"""

    available = False

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        raise AssertionError("强模型未配置时不该发起调用")

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


class _Slow(LLMProvider):
    """慢到超时的强模型：每次调用先睡一段（用真实 ``asyncio.sleep``，配一个极小的预算）。"""

    def __init__(self, seconds: float = 0.2) -> None:
        self.seconds = seconds
        self.calls = 0

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls += 1
        await asyncio.sleep(self.seconds)
        raise AssertionError("睡醒之后不该真的交回答案")

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


def _covered() -> dict:
    return {"queries": []}


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


def _run(
    *,
    blocks: object | None = None,
    fast: LLMProvider | None = None,
    strong: LLMProvider | None = None,
    search: _Search | None = None,
    sub_search_degraded: Sequence[str] = (),
) -> tuple[research.ResearchOutcome, _Scripted, LLMProvider, _Search]:
    """跑一次完整五步：``fast`` 是规划 / 判断（缺省脚本），``strong`` 是生成。"""

    probe = search if search is not None else _two_rows()
    planner = fast if fast is not None else _Scripted(_plan(), _covered())
    generator = (
        strong
        if strong is not None
        else _Scripted(
            blocks if blocks is not None else _blocks(_block("3 个工作日内到账。", primary=(2,)))
        )
    )
    outcome = asyncio.run(
        research.run_research(
            query=QUERY,
            tenant_id=TENANT,
            search=probe,
            llm_factory=_factory(planner),
            strong_llm_factory=_factory(generator),
            sub_search_degraded=sub_search_degraded,
        )
    )
    return outcome, planner, generator, probe


# ─────────────────────────────────────────── 正常路径：块 + 渲染串 + 完整度


def test_a_round_produces_citation_blocks_from_the_material_pool() -> None:
    """覆盖全材料 → 每块至少引一条核心片；``answer`` 就是这些块的渲染串。"""

    outcome, planner, strong, _ = _run(blocks=_blocks(_block("3 个工作日内到账。", primary=(2,))))

    assert outcome.answer == "3 个工作日内到账。（E2）"
    assert list(outcome.citations) == [
        {
            "text": "3 个工作日内到账。",
            "evidence_ids": ["E2"],
            "status": "supported",
        }
    ]
    # 三个角色各一次：规划 / 判断 / 生成（ADR-0026 §5 的 3 次调用）。
    assert outcome.model_calls == MAX_MODEL_CALLS == 3
    assert planner.calls == ["ResearchPlan", "FollowupPlan"]
    assert isinstance(strong, _Scripted) and strong.calls == ["AnswerOut"]
    assert outcome.evidence_state == "complete"
    assert outcome.strong_model_fallback is None
    assert outcome.stop_reason is StopReason.EVIDENCE_COMPLETE


def test_a_missing_point_becomes_an_insufficient_block_not_invented_text() -> None:
    """材料只盖住一个要点 → 另一块必须是 ``insufficient``（承认不足），完整度是 ``partial``。"""

    outcome, _, _, _ = _run(
        search=_Search({"退款多久到账": [_row("a")], "退款需要哪些材料": []}),
        blocks=_blocks(
            _block("超期退款需人工审核。", primary=(1,)),
            _block("未找到所需材料的说明。", kind="insufficient"),
        ),
    )

    assert [citation["status"] for citation in outcome.citations] == [
        "supported",
        "insufficient",
    ]
    assert outcome.answer is not None and "材料不足" in outcome.answer
    assert outcome.evidence_state == "partial"


def test_the_finalizer_prompt_carries_materials_without_internal_ids() -> None:
    """提示词里只有序号、标题与摘录——切片 id / 文档 id / 节 id 一个都不进去。"""

    _, _, strong, _ = _run()

    prompt = strong.prompts[0]
    for internal in ("chunk-a", "chunk-b", "doc-a", "section-a"):
        assert internal not in prompt


def test_no_material_means_no_model_call_and_no_answer() -> None:
    """材料池为空 → 不调生成（问了也只能编）：``answer = None``、``evidence_state = missing``。"""

    outcome, planner, strong, _ = _run(search=_Search())

    assert outcome.answer is None
    assert outcome.citations == ()
    assert outcome.evidence_state == "missing"
    assert isinstance(strong, _Scripted) and strong.calls == [], "没有材料就不该问生成"
    assert outcome.model_calls == 2
    assert planner.calls == ["ResearchPlan", "FollowupPlan"]


# ─────────────────────────────────────────── 四条校验的出口：answer 落成 null


def test_an_assertion_without_a_citation_leaves_the_answer_empty() -> None:
    """有结论却不引证据 → 整份答案作废，材料与计划照给。"""

    outcome, _, _, _ = _run(blocks=_blocks(_block("退款 3 个工作日到账。")))

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

    outcome, _, _, _ = _run(blocks=_blocks(_block("3 个工作日内到账。", primary=(9,))))

    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_UNRESOLVED_REFERENCE
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]


def test_an_internal_identifier_in_the_answer_leaves_it_empty() -> None:
    """答案里漏出切片 id → ``answer = null``。"""

    outcome, _, _, _ = _run(
        blocks=_blocks(_block("按 chunk-a 的说法，3 个工作日内到账。", primary=(2,)))
    )

    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_UNRESOLVED_REFERENCE


def test_a_conflict_verdict_leaves_the_answer_empty() -> None:
    """多来源块里替用户裁决 → ``answer = null``（冲突要拆成两块、各自带来源）。"""

    outcome, _, _, _ = _run(
        search=_Search(
            {
                "退款多久到账": [_row("a", document="doc-a")],
                "退款需要哪些材料": [_row("b", document="doc-b")],
            }
        ),
        blocks=_blocks(
            _block("以《退款政策》为准：3 个工作日内到账。", primary=(1,), supporting=(2,))
        ),
    )

    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_CONFLICT_ADJUDICATED


# ─────────────────────────────────────────── 降级：强模型未配置 / 失败 / 超时


def test_a_missing_strong_model_falls_back_to_the_fast_one() -> None:
    """强模型没配 → 直接用快模型生成一次（不白问一次），并标 ``unavailable``。"""

    strong = _Unavailable()
    outcome, planner, _, _ = _run(
        fast=_Scripted(_plan(), _covered(), _blocks(_block("3 个工作日内到账。", primary=(2,)))),
        strong=strong,
    )

    assert outcome.answer == "3 个工作日内到账。（E2）"
    assert outcome.strong_model_fallback == research.STRONG_MODEL_UNAVAILABLE
    # 未配置的那一次没有发生，所以总调用次数仍是 3（规划 + 判断 + 生成）。
    assert outcome.model_calls == MAX_MODEL_CALLS == 3


def test_a_failing_strong_model_falls_back_to_the_fast_one() -> None:
    """强模型调用失败 → 快模型兜底、标记 ``failed``，**不报错**。"""

    strong = _Scripted(RuntimeError("模型服务 502"))
    outcome, _, _, _ = _run(
        fast=_Scripted(_plan(), _covered(), _blocks(_block("3 个工作日内到账。", primary=(2,)))),
        strong=strong,
    )

    assert outcome.answer == "3 个工作日内到账。（E2）"
    assert outcome.strong_model_fallback == research.STRONG_MODEL_FAILED
    assert isinstance(strong, _Scripted) and strong.calls == ["AnswerOut"]
    assert outcome.model_calls == 4


def test_a_timing_out_strong_model_falls_back_to_the_fast_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """强模型超时 → 同样兜底（超时就是一次失败：那条请求已经发出去过）。"""

    monkeypatch.setattr(research, "FINALIZER_TIMEOUT_SECONDS", 0.01)
    strong = _Slow(seconds=0.2)
    outcome, _, _, _ = _run(
        fast=_Scripted(_plan(), _covered(), _blocks(_block("3 个工作日内到账。", primary=(2,)))),
        strong=strong,
    )

    assert outcome.answer == "3 个工作日内到账。（E2）"
    assert outcome.strong_model_fallback == research.STRONG_MODEL_FAILED
    assert strong.calls == 1


def test_the_model_calls_never_exceed_the_ceiling_even_with_the_fallback() -> None:
    """强模型失败、快模型也失败 → 真正发出去的调用不超过含兜底位的上限，``answer = null``。"""

    outcome, _, _, _ = _run(
        fast=_Scripted(_plan(), _covered(), RuntimeError("快模型也挂了")),
        strong=_Scripted(RuntimeError("强模型挂了")),
    )

    assert outcome.model_calls == MAX_MODEL_CALLS_WITH_FALLBACK == 4
    assert outcome.answer is None
    assert outcome.strong_model_fallback == research.STRONG_MODEL_FAILED
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]


def test_an_illegal_strong_output_is_retried_once_on_the_fast_model() -> None:
    """强模型交回不合法结构（不是异常）→ 也算失败，兜底一次；仍不合法就 ``answer = null``。"""

    outcome, _, _, _ = _run(
        fast=_Scripted(_plan(), _covered(), _blocks(_block("3 个工作日内到账。", primary=(2,)))),
        strong=_Scripted({"blocks": "不是列表"}),
    )

    assert outcome.answer == "3 个工作日内到账。（E2）", "结构不合法属于「这次没答出来」"
    assert outcome.strong_model_fallback == research.STRONG_MODEL_FAILED


# ─────────────────────────────────────────── 子检索降级：一因一码、去重


def test_degraded_sub_searches_are_collected_once_per_reason() -> None:
    """子检索自身的降级原因去重后进 ``sub_search_degraded``（一因一码，不拼异常文本）。"""

    outcome, _, _, _ = _run(
        sub_search_degraded=[
            "vector search failed",
            "vector search failed",
            "keyword search failed",
        ]
    )

    assert outcome.sub_search_degraded == ("vector search failed", "keyword search failed")


def test_a_clean_run_has_no_degraded_reason_and_no_fallback() -> None:
    """正常路径两个字段都为空——「没有降级」不能被一个占位串说成有。"""

    outcome, _, _, _ = _run()

    assert outcome.sub_search_degraded == ()
    assert outcome.strong_model_fallback is None
