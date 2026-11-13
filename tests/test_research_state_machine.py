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

from app.providers.llm.base import LLMProvider
from app.services import research
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
        spend_seconds: float = 0.0,
    ) -> None:
        self.rows_for = dict(rows_for or {})
        self.default = list(default)
        self.calls: list[str] = []
        self._clock = clock
        self._spend = spend_seconds

    async def __call__(self, sub_query: str) -> list[dict[str, Any]]:
        self.calls.append(sub_query)
        if self._clock is not None and self._spend:
            # 单次请求「占用了 40 秒」：阶段预算必须在它**回来之后**才判，不能打断它。
            self._clock.advance(self._spend)
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
    """按顺序交出预置输出；记下每次问的是哪个 schema。

    预置项是异常时当场抛出（验降级出口）；否则原样返回给服务收敛。
    """

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if not self._responses:
            raise AssertionError("Research 本票只该问两次模型（规划 + 判断），预置输出用完了")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


def _factory(llm: LLMProvider):
    """协程工厂（与 ``get_llm_provider`` 同形）。"""

    async def _get() -> LLMProvider:
        return llm

    return _get


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


def _covered() -> dict:
    """判断器的预置输出：所有要点都有材料（于是没有补证查询）。"""

    return {"queries": []}


def _followup(query: str = "超期退款要哪些材料", point: int = 2) -> dict:
    return {"queries": [{"point": point, "query": query}]}


def _run(
    *responses: object,
    search: _Search | None = None,
    clock: _Clock | None = None,
) -> tuple[research.ResearchOutcome, _ScriptedLLM, _Search]:
    llm = _ScriptedLLM(*responses)
    probe = search if search is not None else _Search()
    outcome = asyncio.run(
        research.run_research(
            query=QUERY,
            tenant_id=TENANT,
            search=probe,
            llm_factory=_factory(llm),
            clock=clock or _Clock(),
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
    assert MAX_MODEL_CALLS == 3
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
    """七种停止原因就是这七个词：响应、日志、统计读同一份枚举。"""

    assert {reason.value for reason in StopReason} == {
        "evidence_complete",
        "no_new_evidence",
        "max_rounds",
        "duplicate_query",
        "timeout",
        "budget_exhausted",
        "degraded",
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
    assert outcome.model_calls == 2
    assert outcome.searches == 2
    assert llm.calls == ["ResearchPlan", "FollowupPlan"]


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
