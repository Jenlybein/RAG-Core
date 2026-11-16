"""票 #148：受控 Research 的固定五步状态机——规划 → 并行检索 → 判断 → 补证 → 最终回答位。

**只测服务的两个输入**（父 spec #138「Testing Decisions」C 行：Research 吃「注入的检索
内核 + 模型工厂」）：检索内核是记账替身，模型是脚本替身。不发网络、不连库、不读
``.env``。

本文件守的四条性质：

* **五步是固定序列，不是自由循环**：首轮一次、补证至多一次，补证跑完就结束——没有
  「再判断一次、再补一次」的分支，于是延迟与模型调用次数都可预估。
* **硬上限是代码常量**：要点 5 / 首轮查询 3 / 补证查询 2 / 补证轮数 1 / 模型调用 3 /
  阶段预算 90 秒；模块里不出现 ``settings``、不读环境变量。
* **七种停止原因同一套词**：响应、日志、统计读的是同一个枚举，不各写一份字符串。
* **阶段预算是阶段间检查**：它只挡住「还没开始的阶段」，不打断已经发出的单次子检索
  ——那条边界由「子检索把时钟推过 90 秒」的用例断言。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider
from app.services import research
from app.services.citation_blocks import REJECTED_GAP_NOT_DECLARED
from app.services.research_limits import (
    MAX_FOLLOWUP_QUERIES,
    MAX_FOLLOWUP_ROUNDS,
    MAX_FIRST_ROUND_QUERIES,
    MAX_MODEL_CALLS,
    MAX_POINTS,
    PLAN_NAME,
    STAGE_BUDGET_SECONDS,
    StopReason,
)

TENANT = "tenant_demo"
QUERY = "退款多久到账，需要哪些材料"


# ─────────────────────────────────────────────────────────────── 离线替身


def _row(
    key: str,
    *,
    content: str | None = None,
    section: str | None = None,
    title: str = "退款政策",
) -> dict[str, Any]:
    """一条交付单元（内核折叠之后的形状：一节一条，带代表片 id 与章节路径）。"""

    return {
        "document_id": f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "kb_id": "kb-a",
        "title": title,
        "content": content if content is not None else f"{key} 的正文。",
        "score": 0.9,
        "section_id": section if section is not None else f"section-{key}",
        "heading_path": f"退款 > {key}",
    }


class _Search:
    """检索内核替身：按子查询返回预置行，并记下每一次的查询串。

    ``rows_for`` 里没有的查询返回空——「这条子查询没命中」也是一种真实结果。
    """

    def __init__(
        self,
        rows_for: Mapping[str, Sequence[dict[str, Any]]] | None = None,
        *,
        default: Sequence[dict[str, Any]] = (),
        clock: _Clock | None = None,
        spend_for: Mapping[str, float] | None = None,
    ) -> None:
        self.rows_for = dict(rows_for or {})
        self.default = list(default)
        self.calls: list[str] = []
        self._clock = clock
        self._spend_for = dict(spend_for or {})

    async def __call__(self, sub_query: str) -> list[dict[str, Any]]:
        self.calls.append(sub_query)
        spend = self._spend_for.get(sub_query)
        if self._clock is not None and spend:
            # 这一次子检索「占用了一段预算」：阶段预算必须在它**回来之后**才判，
            # 不能打断它（父 spec：阶段间检查，不是硬超时）。
            self._clock.advance(spend)
        return [dict(row) for row in self.rows_for.get(sub_query, self.default)]


class _Clock:
    """可推进的单调时钟（阶段预算的注入缝，不用 sleep 也不用改常量）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedLLM(LLMProvider):
    """按 schema 交出预置输出；记下每次问的是哪个 schema。

    单一 ``default`` 角色下四个阶段共用一份替身：生成阶段（``AnswerOut``）走专门的那一份
    预置输出，规划 / 判定 / 复查（``ResearchPlan`` / ``CoverageJudgement``）按给定的顺序
    队列消费。这样「有没有 ⑤ 复查」不会打乱生成阶段该拿到的东西。

    预置项是异常时当场抛出；``spend`` 让某一次调用「占掉一段预算」——阶段预算是阶段间
    检查，所以**模型调用本身**也要能被推到越线。
    """

    def __init__(
        self,
        *responses: object,
        answer: object | None = None,
        clock: _Clock | None = None,
        spend: Mapping[str, float] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._answer = answer
        self._answer_used = False
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self._clock = clock
        self._spend = dict(spend or {})

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if schema.__name__ == "AnswerOut":
            item = self._answer
            self._answer_used = True
        elif not self._responses and schema.__name__ == "CoverageJudgement":
            # ⑤ 复查：用例若没显式给复查输出，就交回一份「全 supported」的合法判定。
            # 这些用例关心的是「补证跑了几轮 / 有没有多花一次判定」，不是复查的结论；
            # 给缺省值让它们不必各写一份与主题无关的脚本。
            item = _covered()
        else:
            if not self._responses:
                raise AssertionError(f"预置输出用完了：还有一次 {schema.__name__} 没有应答")
            item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        spend = self._spend.get(schema.__name__)
        if self._clock is not None and spend:
            self._clock.advance(spend)
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


def _factory(llm: LLMProvider):
    """协程工厂（与 ``get_llm_provider`` 同形）。"""

    async def _get() -> LLMProvider:
        return llm

    return _get


def _answer_blocks() -> dict:
    """生成阶段的预置输出：一条引用第一条材料的核心片（本文件关心的是步数，不是块）。"""

    return {
        "blocks": [
            {"text": "已按材料给出结论。", "kind": "supported", "primary": [1], "supporting": []}
        ]
    }


def _plan(queries: list[dict[str, Any]] | None = None, points: list[str] | None = None) -> dict:
    """规划器的预置输出：要点 + 首轮查询（每条绑定一个要点序号）。"""

    return {
        "points": points if points is not None else ["退款时限", "所需材料"],
        "queries": queries
        if queries is not None
        else [
            {"point": 1, "query": "退款多久到账"},
            {"point": 2, "query": "退款需要哪些材料"},
        ],
    }


def _covered(points: int = 2, *, material: int = 1) -> dict:
    """覆盖判定的预置输出：所有要点都被同一条材料支撑（于是没有补证查询）。

    票 #164 起判定是**逐要点**的，所以这份替身必须把每一个要点都答一遍——少答一个要点
    整份判定作废（那是 ``test_a_verdict_that_skips_a_point_is_not_accepted`` 守的事）。
    """

    return {
        "points": [
            {"point": index, "status": "supported", "materials": [material]}
            for index in range(1, points + 1)
        ],
        "queries": [],
    }


def _followup(query: str = "超期退款要哪些材料", point: int = 2, *, points: int = 2) -> dict:
    """带一个缺口的判定：``point`` 这个要点判成 ``none``，其余有支撑，并给一条补证查询。"""

    return {
        "points": [
            {
                "point": index,
                "status": "none" if index == point else "supported",
                "materials": [] if index == point else [1],
            }
            for index in range(1, points + 1)
        ],
        "queries": [{"point": point, "query": query}],
    }


def _run(
    *responses: object,
    search: _Search | None = None,
    clock: _Clock | None = None,
    spend: Mapping[str, float] | None = None,
    strong: LLMProvider | None = None,
) -> tuple[research.ResearchOutcome, _ScriptedLLM, _Search]:
    """跑一次五步：单一 ``default`` 角色，四阶段共用一份脚本替身。

    ``responses`` 是**规划 + 判断**的预置输出；生成阶段缺省补一个合法块。第 13 步删掉了
    第二个模型工厂，生成不再单独占一份替身——但 ``spend`` 仍按 schema 名计数，
    ``AnswerOut`` 与 ``ResearchPlan`` / ``CoverageJudgement`` 不同名，所以预算用例不受影响。
    """

    probe = search if search is not None else _Search()
    running_clock = clock or _Clock()
    if strong is None:
        answer: object = _answer_blocks()
    elif isinstance(strong, _ScriptedLLM):
        # ``_partial_recovery_run`` 传的是一份脚本替身：取它的第一条预置输出。
        answer = strong._responses[0]
    else:
        answer = strong
    llm = _ScriptedLLM(*responses, answer=answer, clock=running_clock, spend=spend)
    outcome = asyncio.run(
        research.run_research(
            query=QUERY,
            tenant_id=TENANT,
            search=probe,
            llm_factory=_factory(llm),
            clock=running_clock,
        )
    )
    return outcome, llm, probe


# ───────────────────────────────────────────────────────── 六条硬上限与七种停止原因


def test_the_six_caps_are_code_constants() -> None:
    """六条上限写死在模块里（父 spec：不走环境变量）——数字本身就是契约。"""

    assert MAX_POINTS == 5
    assert MAX_FIRST_ROUND_QUERIES == 3
    assert MAX_FOLLOWUP_QUERIES == 2
    assert MAX_FOLLOWUP_ROUNDS == 1
    # 3 → 4（#165）：补证捞到材料之后要复查一次，见 ADR-0029 实施备注第 2 条。
    assert MAX_MODEL_CALLS == 4
    assert STAGE_BUDGET_SECONDS == 90
    assert PLAN_NAME == "research_fixed"


def test_the_state_machine_never_reads_settings_or_the_environment() -> None:
    """上限与状态机不读部署配置：同一份代码在任何机器上烧的钱一样。

    与内核那条守卫同一个做法——读源码是唯一能证明「这条约束不是靠自觉」的方式。
    """

    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("research.py", "research_limits.py"):
        source = (root / "app" / "services" / name).read_text(encoding="utf-8")
        for forbidden in ("from app.core.config import", "os.environ", "os.getenv", "settings."):
            assert forbidden not in source, f"{name} 不该出现 {forbidden}"


def test_the_seven_stop_reasons_are_one_vocabulary() -> None:
    """六种停止原因就是这六个词：响应、日志、统计读同一份枚举。

    第 13 步退役 ``degraded``：模型调用失败整单 50003，不再有降级出口。
    """

    assert {reason.value for reason in StopReason} == {
        "evidence_complete",
        "no_new_evidence",
        "max_rounds",
        "duplicate_query",
        "timeout",
        "budget_exhausted",
    }


# ───────────────────────────────────────────────────────── ① ② ③ ④ 五步


def test_a_covered_question_stops_after_the_first_round() -> None:
    """全覆盖：规划两个要点、两条查询各跑一次内核，判断说「齐了」→ 不补证。

    这里同时断言「每条查询绑定要点」：响应里的计划是 ``point_id + query`` 的配对，
    不是一个要点列表加一个互不相干的查询列表。
    """

    rows = [_row("a"), _row("b")]
    search = _Search({"退款多久到账": [rows[0]], "退款需要哪些材料": [rows[1]]})

    outcome, llm, probe = _run(_plan(), _covered(), search=search)

    assert probe.calls == ["退款多久到账", "退款需要哪些材料"]
    assert [point.point_id for point in outcome.points] == ["A1", "A2"]
    assert [(item.point_id, item.query, item.round) for item in outcome.queries] == [
        ("A1", "退款多久到账", 1),
        ("A2", "退款需要哪些材料", 1),
    ]
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]
    assert outcome.stop_reason is StopReason.EVIDENCE_COMPLETE
    assert outcome.followup_rounds == 0
    # 规划 + 判断 + 生成各一次（票 #149 起第 ⑤ 步会真的花掉第三次调用）。
    assert outcome.model_calls == 3
    assert outcome.searches == 2
    assert llm.calls == ["ResearchPlan", "CoverageJudgement", "AnswerOut"]


def test_a_multi_point_question_gets_two_rounds_of_materials() -> None:
    """有一个要点没材料 → 一条定向补证查询，材料来自两轮（首轮 2 条 + 补证 1 条）。"""

    first = [_row("a"), _row("b")]
    follow = [_row("c")]
    search = _Search(
        {
            "退款多久到账": [first[0]],
            "退款需要哪些材料": [first[1]],
            "超期退款要哪些材料": follow,
        }
    )

    outcome, _, probe = _run(_plan(), _followup(), search=search)

    assert probe.calls == ["退款多久到账", "退款需要哪些材料", "超期退款要哪些材料"]
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b", "chunk-c"]
    # 两轮材料：计划里既有首轮的查询，也有补证那条，各自带轮次。
    assert {(item.query, item.round) for item in outcome.queries} == {
        ("退款多久到账", 1),
        ("退款需要哪些材料", 1),
        ("超期退款要哪些材料", 2),
    }
    assert outcome.followup_rounds == MAX_FOLLOWUP_ROUNDS == 1
    # 证据增加了，但轮次已经用尽：诚实的停止原因是「轮数用尽」而不是「齐了」。
    assert outcome.stop_reason is StopReason.MAX_ROUNDS
    # 补证真的捞到了材料（上面 ``chunk-c``），所以**多了一次 ⑤ 复查**：
    # 规划 + 判断 + 复查 + 生成 = 4。这是 #165 的决定（ADR-0029 实施备注第 2 条），
    # 不是顺手把数字调大。
    assert outcome.model_calls == 4


def test_a_followup_that_adds_nothing_stops_with_no_new_evidence() -> None:
    """补证只捞到已经有的那一条 → ``no_new_evidence``，材料不重复记两遍。"""

    rows = [_row("a"), _row("b")]
    search = _Search(
        {
            "退款多久到账": [rows[0]],
            "退款需要哪些材料": [rows[1]],
            # 补证捞回来的还是已经交付过的一节（换了查询、同一节）。
            "超期退款要哪些材料": [rows[0]],
        }
    )

    outcome, _, _ = _run(_plan(), _followup(), search=search)

    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]
    assert outcome.stop_reason is StopReason.NO_NEW_EVIDENCE
    assert outcome.followup_rounds == 1


def test_a_duplicate_followup_query_is_never_sent_twice() -> None:
    """判断器把一条首轮已经跑过的查询又交回来 → 不重跑，``duplicate_query``。"""

    search = _Search({"退款多久到账": [_row("a")], "退款需要哪些材料": [_row("b")]})

    outcome, _, probe = _run(_plan(), _followup(query="退款需要哪些材料"), search=search)

    assert probe.calls == ["退款多久到账", "退款需要哪些材料"], "重复查询不许再烧一次内核"
    assert outcome.stop_reason is StopReason.DUPLICATE_QUERY
    assert outcome.followup_rounds == 0


# ───────────────────────────────────────────────────── 六条硬上限：各撞一次


def test_the_point_cap_truncates_the_plan_to_five_points() -> None:
    """模型给了 6 个要点 → 计划里只留 5 个（截断后的计划就是实际执行的那一份）。

    **票 #164 起这条用例的形状变了**（不是放宽，是变严）：每个要点都配了一条查询，于是
    「截断到 5 个要点」与「首轮只执行 3 条查询」在同一次断言里同时成立——被截掉的那个要点
    连它的查询一起消失，而留下的 5 个要点都各自有一条查询（覆盖率式的旧写法只断言前 5 个
    要点还在，说不出「它们有没有被检索过」）。
    """

    points = ["P1", "P2", "P3", "P4", "P5", "P6"]
    plan = _plan(
        points=points,
        queries=[{"point": index, "query": f"查询{index}"} for index in range(1, 6)],
    )

    outcome, _, probe = _run(
        plan, _covered(points=MAX_POINTS), search=_Search(default=[_row("a")])
    )

    assert [point.text for point in outcome.points] == points[:MAX_POINTS]
    assert len(outcome.points) == MAX_POINTS == 5
    assert probe.calls == [f"查询{index}" for index in range(1, MAX_FIRST_ROUND_QUERIES + 1)]


def test_the_first_round_cap_keeps_at_most_three_searches() -> None:
    """模型给了 4 条首轮查询 → 只跑前 3 条；计划里也只有这 3 条（没有「跑过但没记」）。"""

    plan = _plan(
        queries=[
            {"point": 1, "query": "查询一"},
            {"point": 1, "query": "查询二"},
            {"point": 2, "query": "查询三"},
            {"point": 2, "query": "查询四"},
        ]
    )

    outcome, _, probe = _run(plan, _covered(), search=_Search(default=[_row("a")]))

    assert probe.calls == ["查询一", "查询二", "查询三"]
    assert len(probe.calls) == MAX_FIRST_ROUND_QUERIES == outcome.searches == 3
    assert [item.query for item in outcome.queries] == ["查询一", "查询二", "查询三"]


def test_the_followup_cap_keeps_at_most_two_searches() -> None:
    """判定给了 3 条补证查询 → 只跑前 2 条（成本上限对补证轮同样成立）。"""

    judgement = {
        "points": [
            {"point": 1, "status": "none", "materials": []},
            {"point": 2, "status": "none", "materials": []},
        ],
        "queries": [
            {"point": 1, "query": "补一"},
            {"point": 2, "query": "补二"},
            {"point": 1, "query": "补三"},
        ],
    }

    outcome, _, probe = _run(_plan(), judgement, search=_Search(default=[_row("a")]))

    assert probe.calls == ["退款多久到账", "退款需要哪些材料", "补一", "补二"]
    assert len([item for item in outcome.queries if item.round == 2]) == MAX_FOLLOWUP_QUERIES == 2


def test_the_model_call_cap_blocks_a_new_stage_instead_of_a_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型调用次数用尽 → 不再进入判断阶段；**已经到手的材料照给**（``budget_exhausted``）。

    这条上限在 #148 里有意义地撞得到：本票只用两次调用（规划 + 判断），第三次留给 #149 的
    生成。把上限压到 1 就是「规划用掉了唯一一次预算」的真实形状。
    """

    monkeypatch.setattr(research, "MAX_MODEL_CALLS", 1)
    search = _Search(default=[_row("a")])

    outcome, llm, probe = _run(_plan(), _covered(), search=search)

    assert outcome.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert outcome.model_calls == 1
    assert llm.calls == ["ResearchPlan"], "预算用尽后不许再问判断"
    assert probe.calls == ["退款多久到账", "退款需要哪些材料"], "首轮检索照跑"
    # 两条子查询命中同一节：材料只交付一次（材料是并集，不是每次子检索的原始回包）。
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a"]


# ─────────────────────────────────────────────── 阶段预算：只挡新阶段，不中断单次请求


def test_the_stage_budget_never_interrupts_a_request_already_sent() -> None:
    """一次子检索把时钟推过 90 秒 → **它的材料照收**，被挡住的是下一个阶段。

    这正是「阶段预算 = 阶段间检查，不是硬超时」的可断言形态：中断它等于把一次已经付过钱、
    可能只差 200 毫秒就回来的请求丢掉，再记一条 ``timeout``——花钱买一次失败。
    """

    clock = _Clock()
    rows = [_row("a"), _row("b")]
    search = _Search(
        {"退款多久到账": [rows[0]], "退款需要哪些材料": [rows[1]]},
        clock=clock,
        # 首轮两条查询**并行**发出：各 50 秒，合计把预算推过 90 秒。
        spend_for={"退款多久到账": 50.0, "退款需要哪些材料": 50.0},
    )

    outcome, llm, probe = _run(_plan(), _covered(), search=search, clock=clock)

    assert probe.calls == ["退款多久到账", "退款需要哪些材料"], "已发出的两条都跑完了"
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]
    assert outcome.stop_reason is StopReason.TIMEOUT
    assert outcome.model_calls == 1
    assert llm.calls == ["ResearchPlan"], "预算用尽后不许再进判断阶段"


def test_the_stage_budget_also_guards_the_final_answer_slot() -> None:
    """补证跑完才越线 → 补证的材料照样在，被挡住的是最终回答位（#149 的那一步）。"""

    clock = _Clock()
    search = _Search(
        {
            "退款多久到账": [_row("a")],
            "退款需要哪些材料": [_row("b")],
            "超期退款要哪些材料": [_row("c")],
        },
        clock=clock,
        spend_for={"超期退款要哪些材料": 100.0},
    )

    outcome, llm, _ = _run(_plan(), _followup(), search=search, clock=clock)

    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b", "chunk-c"]
    assert outcome.followup_rounds == 1
    # 补证已经跑完（材料在），所以原因不该是「轮数用尽」：是最终回答位进不去。
    assert outcome.stop_reason is StopReason.TIMEOUT
    assert outcome.answer is None
    assert outcome.citations == ()
    assert llm.calls == ["ResearchPlan", "CoverageJudgement"], "判断已经在越线之前跑过了"


# ─────────────────────────────────────────── 越界防护：模型只能点已存在的要点序号


@pytest.mark.parametrize(
    ("plan", "judgement"),
    [
        # 查询绑到不存在的要点：规划输出不合法。
        (_plan(points=["退款时限"], queries=[{"point": 2, "query": "退款多久到账"}]), _covered()),
        # 判断器点了一个不存在的要点：判定输出不合法。
        (_plan(), _followup(point=9)),
        # 只有要点、没有查询的计划：规划输出不合法。
        (_plan(queries=[]), _covered()),
    ],
    ids=["plan_bad_point", "judgement_bad_point", "plan_no_query"],
)
def test_an_illegal_model_output_is_a_50003(plan: object, judgement: object) -> None:
    """规划 / 判定输出不合法 = 模型答了但形状不对 → 整单 50003（客户重试）。

    旧行为是「退回原问题 / 停止补证 + stop_reason=degraded」；第 13 步删掉降级出口，
    这些形状不合法的输入与模型抛异常走同一个可重试的码。
    """

    with pytest.raises(AppError) as caught:
        _run(plan, judgement, search=_Search(default=[_row("a")]))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_the_judgement_prompt_carries_materials_without_internal_ids() -> None:
    """判断提示词里只有序号、标题、章节路径与摘录——没有切片 id / 文档 id。"""

    rows = [_row("a", content="超期退款需人工审核。")]
    search = _Search({"退款多久到账": rows, "退款需要哪些材料": []})

    outcome, llm, _ = _run(_plan(), _covered(), search=search)

    prompt = llm.prompts[1]
    assert "超期退款需人工审核。" in prompt
    assert "A1" in prompt and "A2" in prompt
    for internal in ("chunk-a", "doc-a", "section-a"):
        assert internal not in prompt
    assert outcome.stop_reason is StopReason.EVIDENCE_COMPLETE


# ─────────────────────────── 第 13 步：模型调用失败即整单 50003（无降级出口）


@pytest.mark.parametrize(
    ("plan", "judgement", "expected_schema_calls"),
    [
        (RuntimeError("模型服务 502"), _covered(), ["ResearchPlan"]),
        (_plan(), RuntimeError("模型超时"), ["ResearchPlan", "CoverageJudgement"]),
    ],
    ids=["planner_exception", "decider_exception"],
)
def test_a_model_exception_at_any_stage_is_a_50003(
    plan: object, judgement: object, expected_schema_calls: list[str]
) -> None:
    """规划 / 判定抛异常 → 整单 50003；不再退回原问题、不再标 degraded。"""

    with pytest.raises(AppError) as caught:
        _run(plan, judgement, search=_Search(default=[_row("a")]))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def test_the_stage_budget_is_checked_before_the_followup_round() -> None:
    """判断这一步自己越线 → 补证那条查询**一次都不发**（阶段间检查的另一个边界）。"""

    clock = _Clock()
    search = _Search({"退款多久到账": [_row("a")], "退款需要哪些材料": [_row("b")]})

    outcome, llm, probe = _run(
        _plan(),
        _followup(),
        search=search,
        clock=clock,
        spend={"CoverageJudgement": 95.0},
    )

    assert probe.calls == ["退款多久到账", "退款需要哪些材料"], "补证阶段根本没开始"
    assert outcome.followup_rounds == 0
    assert outcome.stop_reason is StopReason.TIMEOUT
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a", "chunk-b"]
    assert llm.calls == ["ResearchPlan", "CoverageJudgement"]


# ─────────────────────────────────────────── 检索本身的故障不披成 code 0


def test_a_failing_sub_search_is_not_swallowed() -> None:
    """子检索失败**不**降级成「材料为空 + code 0」：如实上抛（ADR-0028 的边界）。

    Research 的降级出口只覆盖三个模型阶段。检索本身不可用（关键词引擎没配、库不可见、
    嵌入服务挂了）意味着这套集成的检索坏了——把它包成一份「看起来成功、材料是空的」
    响应，会让调用方以为「查过了没有」。
    """

    async def boom(_sub_query: str) -> list[dict[str, Any]]:
        raise AppError(ErrorCode.RETRIEVAL_FAILED)

    with pytest.raises(AppError) as caught:
        asyncio.run(
            research.run_research(
                query=QUERY,
                tenant_id=TENANT,
                search=boom,
                llm_factory=_factory(_ScriptedLLM(_plan(), _covered())),
                clock=_Clock(),
            )
        )

    assert caught.value.code is ErrorCode.RETRIEVAL_FAILED


# ─────────────────────────────────────────── ⑤ 复查（#165 的结论，ADR-0029 实施备注第 2 条）


def _rejudged(*, point: int = 2, material: int = 3) -> dict:
    """复查的预置输出：原来缺的那个要点，现在由**补证捞回来的那条材料**撑住。"""

    return {
        "points": [
            {
                "point": index,
                "status": "supported",
                "materials": [material] if index == point else [1],
            }
            for index in range(1, 3)
        ],
        "queries": [],
    }


def _supplemented_search() -> _Search:
    """首轮两条各自命中，补证那条命中**新材料** chunk-c。"""

    return _Search(
        {
            "退款多久到账": [_row("a")],
            "退款需要哪些材料": [_row("b")],
            "超期退款要哪些材料": [_row("c")],
        }
    )


def test_a_recovered_point_is_judged_again_instead_of_staying_unknown() -> None:
    """补证捞到材料 → **再判一次**，``unknown`` 落成结论。

    ``unknown`` 两边都不落地（不进 ``gaps``、也不代表有支撑），而缺口闸是文本级的，看不见
    它——不复查就等于「这条材料既不算数也没人管」。
    """

    outcome, llm, _ = _run(_plan(), _followup(), _rejudged(), search=_supplemented_search())

    statuses = {item.point_id: item.status for item in outcome.coverage}
    assert statuses == {"A1": "supported", "A2": "supported"}
    assert outcome.gaps == ()
    # 规划 + 判断 + **复查** + 生成。
    assert outcome.model_calls == 4
    assert llm.calls == ["ResearchPlan", "CoverageJudgement", "CoverageJudgement", "AnswerOut"]


def test_the_rejudged_point_keeps_pointing_at_the_newly_added_material() -> None:
    """复查给出的证据编号要指向**补回来的那条**，不是在原有材料里随便挑一条。"""

    outcome, _, _ = _run(_plan(), _followup(), _rejudged(), search=_supplemented_search())

    recovered = next(item for item in outcome.coverage if item.point_id == "A2")
    assert recovered.evidence_ids == ("E3",)


def test_a_point_that_was_supported_never_gets_downgraded_by_the_second_round() -> None:
    """合并是**单调**的：第一轮判成 ``supported`` 的，第二轮说 ``none`` 也不采纳。

    理由是材料只增不减——上一轮能从池子里读出它，这一轮池子更大，没有理由读不出。让第二轮
    有权推翻第一轮，等于给「同一份材料两次判定不一致」开口子，而 ``coverage`` 里没有字段能
    表达这种不一致。
    """

    second_says_none = {
        "points": [
            {"point": 1, "status": "none", "materials": []},
            {"point": 2, "status": "supported", "materials": [3]},
        ],
        "queries": [],
    }

    outcome, _, _ = _run(_plan(), _followup(), second_says_none, search=_supplemented_search())

    statuses = {item.point_id: item.status for item in outcome.coverage}
    assert statuses == {"A1": "supported", "A2": "supported"}


def test_a_followup_that_changed_nothing_does_not_spend_a_judgement() -> None:
    """补证一条新材料都没捞到 → **不复查**（那些要点是真缺口，没有可复查的对象）。"""

    outcome, llm, _ = _run(
        _plan(),
        _followup(),
        search=_Search(
            {
                "退款多久到账": [_row("a")],
                "退款需要哪些材料": [_row("b")],
                "超期退款要哪些材料": [],
            }
        ),
    )

    assert outcome.stop_reason is StopReason.NO_NEW_EVIDENCE
    assert outcome.gaps == ("A2",)
    assert outcome.model_calls == 3
    assert llm.calls == ["ResearchPlan", "CoverageJudgement", "AnswerOut"]


def test_a_failed_rejudge_is_a_model_call_failure() -> None:
    """⑤ 复查的模型调用失败 → 整单 50003（第 13 步），不再保持 unknown 假装跑完了。"""

    with pytest.raises(AppError) as caught:
        _run(
            _plan(),
            _followup(),
            RuntimeError("复查也挂了"),
            search=_supplemented_search(),
        )

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


def _gap_answer() -> dict:
    """生成阶段的预置输出：一条 ``supported`` + 一条 ``insufficient``（渲染串里有「材料不足」）。"""

    return {
        "blocks": [
            {"text": "已按材料给出结论。", "kind": "supported", "primary": [1], "supporting": []},
            {"text": "未找到所需材料的说明。", "kind": "insufficient"},
        ]
    }


def _three_point_judge() -> dict:
    """第一轮判定：A1 有支撑，A2 与 A3 都缺，各自给一条补证查询。"""

    return {
        "points": [
            {"point": 1, "status": "supported", "materials": [1]},
            {"point": 2, "status": "none", "materials": []},
            {"point": 3, "status": "none", "materials": []},
        ],
        "queries": [
            {"point": 2, "query": "补证二"},
            {"point": 3, "query": "补证三"},
        ],
    }


def _partial_recovery_run(*, declare_gap: bool):
    """三个要点：A2 补证**没**捞到材料（真缺口），A3 补证捞到了（→``unknown``）。"""

    plan = _plan(
        points=["要点一", "要点二", "要点三"],
        queries=[
            {"point": 1, "query": "查询一"},
            {"point": 2, "query": "查询二"},
            {"point": 3, "query": "查询三"},
        ],
    )
    search = _Search(
        {
            "查询一": [_row("a")],
            # A2 的补证**什么都没捞到** —— 于是它是「查过，没有」的真缺口。
            "查询二": [],
            "补证二": [],
            # A3 的补证捞到新材料 → 它变成 unknown，复查才有活干。
            "查询三": [],
            "补证三": [_row("c")],
        }
    )
    # 复查：A3 有支撑；而它顺手把 A2 也说成有支撑——这是要挡住的「缺口静默消失」。
    second = {
        "points": [
            {"point": 1, "status": "supported", "materials": [1]},
            {"point": 2, "status": "supported", "materials": [2]},
            {"point": 3, "status": "supported", "materials": [2]},
        ],
        "queries": [],
    }
    generator = _ScriptedLLM(_gap_answer() if declare_gap else _answer_blocks())
    return _run(plan, _three_point_judge(), second, search=search, strong=generator)


def test_a_point_whose_followup_found_nothing_stays_a_gap_after_the_rejudge() -> None:
    """``none`` **不因复查翻案**：它的定向补证已经跑过，没捞到材料 = 「查过，没有」。

    这是复查里最容易写错的一处：如果合并规则只护住 ``supported``，那么一个新样本就能把真缺口
    改成 ``supported``，**缺口静默消失**——而缺口闸正是靠 ``gaps`` 工作的。
    """

    outcome, _, _ = _partial_recovery_run(declare_gap=True)

    statuses = {item.point_id: item.status for item in outcome.coverage}
    assert statuses == {"A1": "supported", "A2": "none", "A3": "supported"}
    assert outcome.gaps == ("A2",)


def test_the_gap_that_survives_the_rejudge_still_drives_the_gate() -> None:
    """复查之后仍存在的缺口，照样要过缺口闸——这是加 ⑤ 之后**唯一**没被覆盖的那条路径。"""

    outcome, _, _ = _partial_recovery_run(declare_gap=False)

    assert outcome.gaps == ("A2",)
    assert outcome.answer is None
    assert outcome.answer_rejected_reason == REJECTED_GAP_NOT_DECLARED
