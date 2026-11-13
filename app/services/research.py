"""受控 Research：固定五步状态机（第 11 步票 #148 / #149）。

```text
① 规划        问题 → ≤5 个要点 + ≤3 条首轮查询（每条查询绑定一个要点）
      │ 失败 → 退回原问题跑一次普通检索，code 0，停止原因 degraded
② 并行检索    每条查询各跑一次「检索内核」（asyncio.gather）
      │
③ 判断        哪些要点还没有材料 → ≤2 条定向补证查询
      │ 失败 → 停止补证，材料照给，停止原因 degraded
④ 补证        ≤1 轮，并行执行、合并新材料
      │
⑤ 最终回答    最终材料池 → 引用块（只能点下标）→ 四条硬校验 → 任一不过 answer = null
      │ 强模型未配 / 失败 / 超时 → 快模型兜底一次，并在 metadata 标记（不报错）
```

**这是状态机，不是自由循环**：五步按固定顺序各走一次，补证跑完就结束。于是「最慢多久、
最多几次模型调用、最多几次检索」全部可以从 :mod:`app.services.research_limits` 的常量
推出来。反过来做（「不够就再判断一次」）会让延迟与成本取决于模型这一次的心情，而父 spec
#138 把「延迟与成本可预估」写成了验收项。

**它吃「注入的检索内核 + 模型工厂」两个输入**（ADR-0028 §4）：检索内核是
``Callable[[str], Awaitable[...]]``——一次子查询一次调用；模型工厂与切片编排同形
（``get_llm_provider("fast")``）。于是五步的每条降级出口、六条硬上限与七种停止原因都能
脱开真实检索与真实模型被断言（``tests/test_research_state_machine.py``）。

**⑤ 的门槛在另一个模块**（票 #149）：块的结构、四条硬校验、渲染串与三态完整度全在
:mod:`app.services.citation_blocks`，本模块只负责「什么时候问、问哪一份材料、拿到不合法的
东西怎么退」。生成用**强模型角色**：它要准，不要快；未配置 / 失败 / 超时换快模型兜底一次，
标记进 ``metadata.strong_model_fallback``——那是成本与质量之间一次**看得见**的交换，
不是一次静默降级。

**子查询不进改写、不写归属索引、不记账、不发观测**：这四件事一件都不在本模块里，也不在
它调的内核里（ADR-0028 §2 的五件事全在外层，由 HTTP 入口做一次）。判据只有一句——「每条
子查询都要各做一次，还是每次调用只应做一次」。一次 Research 的身份是一行归属索引、一份
trace，子检索在它里面以阶段出现；归属索引那一行因此写在外层（``research_gate``），本模块
只把「子检索自己降级了哪几条原因」原样收上来（``sub_search_degraded``），供外层与响应共用。

**材料只有一个来源：内核交回的交付单元**。模型只写「要点」「查询」与「块的一句话」三种
文本，它返回的任何正文一律丢弃（材料正文逐字来自库，ADR-0026 §4）。

**任何阶段都不许让整单报错**：规划失败、判断失败、生成失败都收敛成「材料照给 + 一条停止
原因（+ 一条可读的元数据）」。但是**子检索失败不在此列**——它意味着这套集成的检索本身
不可用（关键词引擎没配、库不可见、嵌入服务挂了），如实上抛比包成一份「看起来成功、材料
是空的」响应诚实得多。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog
from pydantic import BaseModel, Field

from app.services.citation_blocks import (
    COMPLETENESS_MISSING,
    FINALIZER_SYSTEM,
    REJECTED_EMPTY_BLOCK,
    REJECTED_MALFORMED,
    AnswerOut,
    AnswerRejected,
    FinalAnswer,
    build_finalizer_prompt,
    finalize_answer,
)
from app.services.research_limits import (
    FINALIZER_TIMEOUT_SECONDS,
    MAX_FOLLOWUP_QUERIES,
    MAX_FIRST_ROUND_QUERIES,
    MAX_MODEL_CALLS,
    MAX_MODEL_CALLS_WITH_FALLBACK,
    MAX_POINTS,
    STAGE_BUDGET_SECONDS,
    StopReason,
)
from app.services.trace_identity import tenant_fingerprint

logger = structlog.get_logger(__name__)

#: 注入的检索内核：**一次子查询一次调用**，返回交付单元（折叠之后的一条 = 一节）。
#: 它自己不写归属索引、不记账、不发观测——那是外层每次调用各一次的活（ADR-0028）。
SearchCallable = Callable[[str], Awaitable[Sequence[Mapping[str, Any]]]]

#: 模型工厂（协程）：与切片编排的 ``llm_factory`` 同形。
LLMFactory = Callable[[], Awaitable[Any]]

#: 单调时钟的注入缝：阶段预算要能被用例推到 90 秒之外，而不必真的等 90 秒。
Clock = Callable[[], float]

#: 判断阶段的提示词预算（字符）：材料再长也不能把一次判断变成一次读整库。
#: 它**不是**六条硬上限之一（那六条是成本契约），只是提示词长度的上限。
MAX_DECIDER_MATERIAL_CHARS = 12000

#: 强模型兜底的两个原因码（**一因一码的固定串**，不拼异常文本）。它们进响应的
#: ``metadata.strong_model_fallback``：调用方据此知道「这一次不是强模型答的」。
#:
#: 两件事分成两码，是因为它们的处置不同：``unavailable`` 说明这台机器上就没装强模型
#: （换模型是对的、也不该报警），``failed`` 说明装了但这次没答出来（要进统计与告警）。
#:
#: 它说的是**强模型这一路**为什么没用上，不是「兜底跑成功了」：兜底有没有跑成，读
#: ``model_calls``（第 4 格是否被用掉）与 ``answer`` 是否为空即可——两个字段都是各自
#: 事实的唯一来源，不再多编一个码去表达它们的组合。
STRONG_MODEL_UNAVAILABLE: Final[str] = "strong model unavailable"
STRONG_MODEL_FAILED: Final[str] = "strong model failed"

#: 这些拒绝码来自「结构本身不对」：它不是一次「模型答了但不能发」，而是一次「没答出来」，
#: 因此值得换一个模型再问一次。四条硬校验的码不在此列——那些是「答了，但结论不能发」，
#: 换模型重问等于用成本换概率，而 ADR-0026 要的是整单不生成。
_STRUCTURAL_REJECTIONS: Final[frozenset[str]] = frozenset({REJECTED_MALFORMED, REJECTED_EMPTY_BLOCK})


class _AttemptFailed(Exception):
    """一次生成尝试没拿到可用的结构化输出（结构不对 / 超时）。只在本模块内部流转。"""



# ─────────────────────────────────────────────────────────── 结构化输出（模型只给两种文本）


class PlannedQuery(BaseModel):
    """一条查询 + 它绑定的要点序号（1-based，指向本次要点清单）。

    「绑定」是**结构性**的：模型只能点已经由它自己列出的要点序号，点不到别处去。于是
    「这条查询是为了补哪一块」不需要模型再用自然语言交代——那正是最容易被注入的内容。
    """

    point: int
    query: str


class ResearchPlan(BaseModel):
    """规划阶段的结构化输出：要点 + 首轮查询。**没有答案，没有正文。**"""

    points: list[str] = Field(default_factory=list)
    queries: list[PlannedQuery] = Field(default_factory=list)


class FollowupPlan(BaseModel):
    """判断阶段的结构化输出：只有补证查询。空列表 = 「所有要点都有材料」。

    判据只有一个字段（``queries``）是刻意的：如果同时要它给「哪些要点缺材料」，那两份
    输出就可能互相矛盾（缺 A1 却没有 A1 的查询 / 有 A1 的查询却不在缺失清单里），而矛盾
    之后没有任何一处能裁决。**缺失要点由查询自己表达**。
    """

    queries: list[PlannedQuery] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────── 交回的形状


@dataclass(frozen=True)
class PlanPoint:
    """计划里的一个要点：``A1`` 起编号 + 模型给的一句话（不是正文）。"""

    point_id: str
    text: str


@dataclass(frozen=True)
class PlanQuery:
    """计划里的一条查询：绑定要点 + 轮次（1 首轮 / 2 补证）。

    ``round`` 是「它属于哪一轮」的唯一表达：响应只给轮次计数与每条查询的轮次，**不给**
    每一轮内部的分组结构——那是内部候选池的形状（父 spec：「不返回内部候选池全文」）。
    """

    point_id: str
    query: str
    round: int


@dataclass(frozen=True)
class ResearchOutcome:
    """一次 Research 交回的东西：计划、材料、轮次计数、元数据、**一个**停止原因与最终回答。

    ``answer`` / ``citations`` 是**最终回答位**（票 #149）：``answer`` 是引用块的渲染串，
    ``citations`` 是被四条硬校验放行的那几块。任何一块不过校验 → 两者都回到 ``None`` / 空，
    材料与计划照给（ADR-0026 §3）。

    三个「不丢信息」的字段各有分工，谁也不许吞掉谁：

    * ``evidence_state``：三态完整度（``complete`` / ``partial`` / ``missing``），由核心片
      数算出来——「材料够不够」不能被 ``stop_reason``（那说的是**为什么停**）吞掉；
    * ``strong_model_fallback``：这次生成是不是快模型兜的底，以及为什么（两个固定串之一）；
    * ``sub_search_degraded``：子检索自身降级的去重列表（一因一码，原样上交给外层写行）。

    ``answer_rejected_reason`` 是**内部**字段（不进响应）：它是四条硬校验的拒绝码，用于
    日志与用例断言「哪一种输入被哪一条挡住了」。响应里只有 ``answer = null``——调用方要的
    是「这次不生成」，不是「后端替你判了哪一条」。
    """

    points: tuple[PlanPoint, ...]
    queries: tuple[PlanQuery, ...]
    materials: tuple[dict[str, Any], ...]
    searches: int
    followup_rounds: int
    model_calls: int
    duration_ms: int
    stop_reason: StopReason
    answer: str | None = None
    citations: tuple[dict[str, Any], ...] = ()
    evidence_state: str = COMPLETENESS_MISSING
    strong_model_fallback: str | None = None
    sub_search_degraded: tuple[str, ...] = ()
    answer_rejected_reason: str | None = None


# ─────────────────────────────────────────────────────────── 提示词


_PLANNER_SYSTEM = (
    "你在为一次知识库检索做计划。给定用户的问题：第一，列出回答它需要的要点，"
    f"每个要点一句不超过 30 字的中文，最多 {MAX_POINTS} 个；第二，为这些要点分别给一条"
    f"可以直接拿去检索的查询，最多 {MAX_FIRST_ROUND_QUERIES} 条，每条查询绑定一个要点序号"
    "（从 1 起）。要求：只做检索计划，不要生成答案正文，不要复述材料，不要输出切片 id "
    "或文档 id；查询写成文档里可能出现的陈述句，不要写成向助手提的问句。"
)

_DECIDER_SYSTEM = (
    "你在判断一次检索的材料够不够。给定问题、要点清单（A1 起）与已经检索到的材料（带序号），"
    "找出**还没有材料直接回答**的要点，并为这些要点各给一条定向补证查询，"
    f"最多 {MAX_FOLLOWUP_QUERIES} 条。要求：所有要点都有材料直接回答时，返回空的查询列表；"
    "只输出要点序号与查询文本；不要输出材料正文，不要输出切片 id，不要生成答案。"
)


def build_planner_prompt(*, query: str) -> str:
    """规划阶段的用户消息：**只有原问题**。

    不给它任何材料是刻意的：规划发生在检索之前，此时手里没有任何材料；把「以往的结果」
    塞进来会让它按上一轮的记忆列要点（同一句话两次调用得到两份不一样的计划，而这次没有
    任何东西变了）。
    """

    return f"问题：{query}"


def build_decider_prompt(
    *, query: str, points: Sequence[PlanPoint], materials: Sequence[Mapping[str, Any]]
) -> str:
    """判断阶段的用户消息：问题 + 要点清单 + 已到手的材料（带序号、带预算）。

    材料带序号而不带 id：模型能表达的只有「哪个要点还缺」，不许它指向某一条具体材料
    （那会变成一次隐式的角色分配——那是切片编排的活，不是这里）。
    """

    point_lines = "\n".join(f"{point.point_id}. {point.text}" for point in points)
    return (
        f"问题：{query}\n\n"
        f"要点清单：\n{point_lines}\n\n"
        f"已检索到的材料（共 {len(materials)} 条）：\n{_render_materials(materials)}"
    )


def _render_materials(materials: Sequence[Mapping[str, Any]]) -> str:
    """材料进提示词的形态：序号 + 标题 + 章节路径 + 摘录；正文合计受预算约束。

    截断只影响**提示词**，不影响交回给调用方的材料（``materials`` 里的正文逐字完整）：
    「成本可以截断，材料不许截断」。
    """

    lines: list[str] = []
    budget = MAX_DECIDER_MATERIAL_CHARS
    for index, row in enumerate(materials, start=1):
        if budget <= 0:
            break
        content = str(row.get("content") or "")
        excerpt = content[:budget]
        budget -= len(excerpt)
        heading = str(row.get("heading_path") or "")
        title = str(row.get("title") or "")
        lines.append(f"{index}. 《{title}》{f' · {heading}' if heading else ''}\n{excerpt}")
    return "\n\n".join(lines)


# ─────────────────────────────────────────────────────────── 输出收敛（越界防护）


class _IllegalOutput(Exception):
    """模型输出不合法（结构不对 / 点到不存在的要点 / 查询为空）。只在本模块内部流转。

    「不合法」与「降级」是两件事：这里只负责认出来，**怎么退**由阶段自己决定——规划失败
    退回一次普通检索，判断失败停止补证（父 spec #138 C 段的两条降级出口）。
    """


def _as_int(value: Any) -> int | None:
    """只认真正的整数：``bool`` 是 ``int`` 的子类，得单独挡掉。"""

    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _field(raw: Any, name: str) -> Any:
    """从 ``BaseModel`` 或 ``dict`` 里取一个字段（两条形状都收，调用方不必分叉）。"""

    if isinstance(raw, dict):
        return raw.get(name)
    return getattr(raw, name, None)


def _normalize(text: str) -> str:
    """去重用的规范化：只收空白差异，不改字。

    不做大小写折叠或同义词归并——「这两条查询算不算同一条」如果要做语义判断，判断本身
    就需要一次模型调用，而这一轮的模型调用是有上限的。
    """

    return " ".join(text.split())


def _coerce_points(raw: Any) -> list[str]:
    """要点：非空文本列表；超过上限按上限截断（截断后就是本次真实使用的清单）。"""

    items = _field(raw, "points")
    if not isinstance(items, list):
        raise _IllegalOutput("points is not a list")

    points: list[str] = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else item
        if not isinstance(text, str) or not text.strip():
            raise _IllegalOutput("a point is not a non-empty text")
        points.append(text.strip())
    if not points:
        raise _IllegalOutput("no usable point")
    return points[:MAX_POINTS]


def _coerce_query_items(raw: Any) -> list[tuple[int, str]]:
    """把 ``queries`` 收成「(要点序号, 查询文本)」，结构不对就抛。

    这里**不判序号越界**：越界与否要看要点清单有多长，那是调用方（:func:`_bound_queries`）
    才知道的事。
    """

    items = _field(raw, "queries")
    if items is None:
        raise _IllegalOutput("no queries field")
    if not isinstance(items, list):
        raise _IllegalOutput("queries is not a list")

    pairs: list[tuple[int, str]] = []
    for item in items:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            raise _IllegalOutput("a query entry is not an object")
        point = _as_int(item.get("point"))
        if point is None:
            raise _IllegalOutput("a query has no point number")
        text = item.get("query")
        if not isinstance(text, str) or not text.strip():
            raise _IllegalOutput("a query is empty")
        pairs.append((point, text.strip()))
    return pairs


def _bound_queries(
    pairs: Sequence[tuple[int, str]], *, points: Sequence[PlanPoint], round_no: int
) -> list[PlanQuery]:
    """按要点清单判越界、去重、截断到上限，产出**绑定好的**查询。

    三条判定写在一处，因为它们必须同时成立：

    1. **点到不存在的要点 = 输出不合法。** 序号不在 ``1..len(points)`` 的查询没有归属，
       它既不能进计划（计划里每条查询都绑定要点），也不该被猜成「绑定到第一条」；
    2. **重复查询只留一条。** 同一条查询跑两遍不会得到两份材料（内核是确定性的），
       只会多烧一次检索与一次嵌入；
    3. **超过上限按上限截断。** 上限是成本保证；截断后的清单就是实际执行、也是响应里
       返回的那一份，所以「计划 = 跑过的东西」这件事在结构上成立。
    """

    limit = MAX_FIRST_ROUND_QUERIES if round_no == 1 else MAX_FOLLOWUP_QUERIES
    seen: set[str] = set()
    bound: list[PlanQuery] = []
    for point, text in pairs:
        if not 1 <= point <= len(points):
            raise _IllegalOutput("a query points at a point that does not exist")
        key = _normalize(text)
        if key in seen:
            continue
        seen.add(key)
        bound.append(
            PlanQuery(point_id=points[point - 1].point_id, query=text, round=round_no)
        )
    if len(bound) > limit:
        logger.warning(
            "research queries over the cap", wanted=len(bound), kept=limit, round=round_no
        )
        bound = bound[:limit]
    return bound


def _coerce_plan(raw: Any) -> tuple[list[PlanPoint], list[PlanQuery]]:
    """规划输出 → (要点清单, 首轮查询)。任一处不合法都抛 :class:`_IllegalOutput`。"""

    texts = _coerce_points(raw)
    points = [
        PlanPoint(point_id=f"A{index}", text=text) for index, text in enumerate(texts, start=1)
    ]
    queries = _bound_queries(_coerce_query_items(raw), points=points, round_no=1)
    if not queries:
        # 要点有了、查询一条都没有：这不构成一次检索计划（没法检索）。按规划失败处理，
        # 退回原问题跑一次普通检索——比交回一份「有要点、没材料」的材料诚实。
        raise _IllegalOutput("a plan without queries")
    return points, queries


def _coerce_followup(raw: Any, *, points: Sequence[PlanPoint]) -> list[PlanQuery]:
    """判断输出 → 补证查询（可能为空 = 所有要点都有材料）。"""

    return _bound_queries(_coerce_query_items(raw), points=points, round_no=2)


# ─────────────────────────────────────────────────────────── 材料


def material_key(row: Mapping[str, Any]) -> str:
    """一条材料的身份：优先**节**（交付单元是节，ADR-0013），退化到代表片。

    两条子查询命中同一节时只交付一次——「材料」是给调用方看的那一份，不是每次子检索的
    原始回包。这也是「不返回内部候选池全文」的落地方式：池子留在内层，出去了就只剩并集。
    """

    return str(row.get("section_id") or row.get("chunk_id") or "")


# ─────────────────────────────────────────────────────────── 状态机


async def _default_llm_factory() -> Any:
    """惰性 import：Research 关着时不为一项没开的能力付 import 代价。"""

    from app.providers.llm import get_llm_provider

    return await get_llm_provider("fast")


async def _default_strong_llm_factory() -> Any:
    """生成阶段用的**强模型角色**（``strong``）：要准，不要快（票 #149）。

    与 ``fast`` 同一个工厂、只换角色：装配、缓存、超时、思考强度都由 Provider 那一层管，
    这里不发明第二套。没配 key 时空实现的 ``available`` 为假——那正是「强模型未配置」的
    唯一信号，于是「没装」与「装了但这次失败」在状态机里是两条不同的路。
    """

    from app.providers.llm import get_llm_provider

    return await get_llm_provider("strong")


class _Run:
    """一次 Research 的全部状态与五步的固定序列。

    做成对象而不是一串局部变量：五步之间要共享「已交付的材料」「已跑过的查询」「已经
    用掉几次模型调用」这些事实，而它们**必须只有一个来源**（同一个集合既用于去重又用于
    判重，才不会出现「材料里有、判重时看不见」这类分叉）。
    """

    def __init__(
        self,
        *,
        query: str,
        tenant_id: str,
        search: SearchCallable,
        llm_factory: LLMFactory | None,
        strong_llm_factory: LLMFactory | None,
        sub_search_degraded: Sequence[str],
        clock: Clock,
    ) -> None:
        self.query = query
        self.tenant_id = tenant_id
        self.search = search
        self.llm_factory = llm_factory or _default_llm_factory
        self.strong_llm_factory = strong_llm_factory or _default_strong_llm_factory
        self.clock = clock

        self.points: list[PlanPoint] = []
        self.queries: list[PlanQuery] = []
        self.materials: list[dict[str, Any]] = []
        self.searches = 0
        self.followup_rounds = 0
        self.model_calls = 0
        self.answer: str | None = None
        self.citations: tuple[dict[str, Any], ...] = ()
        self.evidence_state: str = COMPLETENESS_MISSING
        self.strong_model_fallback: str | None = None
        self.answer_rejected_reason: str | None = None

        # 子检索的降级原因是**它自己**交上来的（内核一条固定串，外层收成列表）：这里只
        # 持有那个序列的引用，收尾时才去重——检索发生在 `_stages` 里，构造时它还空着。
        self._sub_search_degraded: Sequence[str] = sub_search_degraded
        self._delivered: set[str] = set()
        self._provider: Any = None
        self._strong_provider: Any = None
        self._started = clock()

    # ── 五步的固定序列 ──

    async def execute(self) -> ResearchOutcome:
        reason = await self._stages()
        # ⑤ 最终回答位：**进入新阶段前的最后一次阶段预算检查**（父 spec 点名的两处之一）。
        # 预算已经用尽时**不进这一阶段**：与前面每一个阶段同一个规矩——阶段间检查挡的是
        # 「还没开始的阶段」，而「生成」就是一个阶段（它还会花掉一次模型调用）。
        gate = self._budget_stop()
        if gate is not None:
            reason = gate
        else:
            await self._finalize()
        return self._outcome(reason)

    async def _stages(self) -> StopReason:
        # ① 规划。失败已经落到「退回原问题跑一次」并记 degraded。
        if not await self._plan():
            return StopReason.DEGRADED

        # ② 并行检索：每条首轮查询各跑一次内核。
        await self._parallel(self.queries)

        # 阶段间检查（判断前）：预算或模型调用用尽 → 不进新阶段，材料照给。
        gate = self._budget_stop()
        if gate is not None:
            return gate

        # ③ 判断。
        decision = await self._decide()
        if decision is None:
            return StopReason.DEGRADED
        if not decision:
            return StopReason.EVIDENCE_COMPLETE

        # 判断给了查询，但全都跑过：一条都不重跑。
        fresh = self._fresh(decision)
        if not fresh:
            return StopReason.DUPLICATE_QUERY

        # 阶段间检查（补证前）。
        gate = self._budget_stop()
        if gate is not None:
            return gate

        # ④ 补证（≤1 轮）。跑完就结束：轮次用尽 = max_rounds，白跑一趟 = no_new_evidence。
        added = await self._parallel(fresh, followup=True)
        return StopReason.MAX_ROUNDS if added else StopReason.NO_NEW_EVIDENCE

    # ── ① 规划 ──

    async def _plan(self) -> bool:
        try:
            raw = await self._call(
                ResearchPlan, _PLANNER_SYSTEM, build_planner_prompt(query=self.query)
            )
            points, queries = _coerce_plan(raw)
        except Exception as exc:  # noqa: BLE001 - 规划失败即降级，绝不整单失败
            logger.warning(
                "research planning failed",
                tenant=tenant_fingerprint(self.tenant_id),
                stage="plan",
                # 只记类型：模型返回的文案可能夹带正文。
                error=type(exc).__name__,
            )
            await self._fallback()
            return False
        self.points = points
        self.queries.extend(queries)
        return True

    async def _fallback(self) -> None:
        """降级出口（一）：**退回原问题执行一次普通检索**，不报错。

        注意这一步仍然是「一次子检索」而不是一次完整检索：改写、记账、归属索引、观测都
        在外层（ADR-0028），降级不该顺手把它们带进来——否则同一次调用会在降级前后各记一
        笔账，而调用方从 ``code 0`` 看不出来。
        """

        self._merge(await self._search_one(self.query))

    # ── ② / ④ 并行检索 ──

    async def _parallel(self, queries: Sequence[PlanQuery], *, followup: bool = False) -> int:
        """并行跑一批查询，返回**新增**材料条数。

        ``asyncio.gather`` 的顺序与入参一致，合并也按这个顺序：同样的输入两次跑出同样的
        材料顺序（``E`` 编号将来要稳定，ADR-0026 §2）。
        """

        batches = await asyncio.gather(*(self._search_one(item.query) for item in queries))
        added = 0
        for batch in batches:
            added += self._merge(batch)
        if followup:
            self.followup_rounds += 1
            self.queries.extend(queries)
        return added

    async def _search_one(self, sub_query: str) -> list[dict[str, Any]]:
        """一次子查询 = 一次内核调用。**不中断**：预算在阶段边界上判，不在这里。"""

        rows = [dict(row) for row in await self.search(sub_query)]
        self.searches += 1
        return rows

    def _merge(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """并入材料池：按节去重；没有身份的行不进材料（它没法被引用）。"""

        added = 0
        for row in rows:
            key = material_key(row)
            if not key or key in self._delivered:
                continue
            self._delivered.add(key)
            self.materials.append(dict(row))
            added += 1
        return added

    # ── ③ 判断 ──

    async def _decide(self) -> list[PlanQuery] | None:
        """判断哪些要点还缺材料。``None`` = 判断失败（停止补证、标记降级、材料照给）。"""

        try:
            raw = await self._call(
                FollowupPlan,
                _DECIDER_SYSTEM,
                build_decider_prompt(
                    query=self.query, points=self.points, materials=self.materials
                ),
            )
            return _coerce_followup(raw, points=self.points)
        except Exception as exc:  # noqa: BLE001 - 判断失败即停止补证，绝不整单失败
            logger.warning(
                "research decision failed",
                tenant=tenant_fingerprint(self.tenant_id),
                stage="decide",
                error=type(exc).__name__,
            )
            return None

    def _fresh(self, queries: Sequence[PlanQuery]) -> list[PlanQuery]:
        """去掉已经跑过的查询（首轮 + 已发生的补证），一条都不重跑。"""

        done = {_normalize(item.query) for item in self.queries}
        return [item for item in queries if _normalize(item.query) not in done]

    # ── ⑤ 最终回答位（#149）──

    async def _finalize(self) -> None:
        """最终回答：只用最终材料池生成引用块，任一硬校验不过就 ``answer = None``。

        四件事按顺序做，顺序不能换：

        1. **没有材料就不问模型**（问了也只能编）：``answer = None``、``evidence_state``
           是 ``missing``——「根本没有材料」与「材料不足」在结构上要能分开；
        2. **问强模型**（未配置 / 失败 / 超时换快模型兜底一次，标记进元数据）；
        3. **过四条硬校验**（:func:`finalize_answer`）：任何一块不过，整份答案作废，
           材料与计划照给——这是 ADR-0026 §3 的默认落点，不是一次失败；
        4. **渲染串与块一起收下**：``answer`` 永远是块的渲染串，没有第二种来源。
        """

        if not self.materials:
            self.evidence_state = COMPLETENESS_MISSING
            return

        prompt = build_finalizer_prompt(query=self.query, materials=self.materials)
        try:
            result = await self._answer_blocks(prompt)
        except AnswerRejected as exc:
            # 四条硬校验之一：**模型答了，但结论不能发**。不换模型重问（那是用成本换概率），
            # 整单不生成——材料与计划仍然完整交回。
            self.answer_rejected_reason = exc.reason
            logger.warning(
                "research answer rejected",
                tenant=tenant_fingerprint(self.tenant_id),
                stage="finalize",
                reason=exc.reason,
            )
            return
        except Exception as exc:  # noqa: BLE001 - 生成失败不整单失败，材料照给
            self.answer_rejected_reason = REJECTED_MALFORMED
            logger.warning(
                "research answer unavailable",
                tenant=tenant_fingerprint(self.tenant_id),
                stage="finalize",
                error=type(exc).__name__,
            )
            return

        self._accept(result)

    def _accept(self, result: FinalAnswer) -> None:
        """把校验过的答案收下：答案是渲染串，块是它唯一的来源。"""

        self.answer = result.answer
        self.evidence_state = result.evidence_state
        self.citations = tuple(
            {
                "text": citation.text,
                "evidence_ids": list(citation.evidence_ids),
                "status": citation.status,
            }
            for citation in result.citations
        )

    async def _answer_blocks(self, prompt: str) -> FinalAnswer:
        """问一次生成；强模型那条路上失败就换快模型兜底**一次**。

        三种「强模型不可用」的处置在这里合流，但**成本不同**，所以不能合并成一件事：

        * **未配置**（``available`` 为假）：一次都不问，直接快模型——白问一次既不计费也
          不该计进成本，但它仍然是一次「没有用强模型」的事实，必须标记；
        * **调用失败 / 超时**：那一次已经发出（可能已经计费），照实计数；再换快模型兜底
          一次。兜底占的是 :data:`MAX_MODEL_CALLS_WITH_FALLBACK` 的第 4 格，没有那一格就
          不兜底——把「上限」当上限，而不是当愿望。
        """

        provider = await self._strong_provider_of_role()
        if provider is None:
            self.strong_model_fallback = STRONG_MODEL_UNAVAILABLE
            return await self._ask_once(await self.llm_factory(), prompt)

        try:
            return await self._ask_once(provider, prompt)
        except AnswerRejected:
            # 硬校验不过：这是「答了但不能发」，不是「没答出来」——不重试。
            raise
        except Exception:
            self.strong_model_fallback = STRONG_MODEL_FAILED
            if self.model_calls >= MAX_MODEL_CALLS_WITH_FALLBACK:
                raise
            return await self._ask_once(await self.llm_factory(), prompt)

    async def _strong_provider_of_role(self) -> Any | None:
        """装配强模型；装不出来或这台机器上没装（``available`` 为假）都返回 ``None``。"""

        if self._strong_provider is not None:
            return self._strong_provider
        try:
            provider = await self.strong_llm_factory()
        except Exception as exc:  # noqa: BLE001 - 装配失败 = 未配置，换快模型
            logger.warning(
                "strong model assembly failed",
                tenant=tenant_fingerprint(self.tenant_id),
                error=type(exc).__name__,
            )
            return None
        if not getattr(provider, "available", True):
            return None
        self._strong_provider = provider
        return provider

    async def _ask_once(self, provider: Any, prompt: str) -> FinalAnswer:
        """一次生成尝试：**先计数再发**（已经发出的调用就算失败也计成本），带单次硬超时。

        超时只在这里发生（见 ``research_limits`` 的模块文档）：生成是最后一步，后面没有
        下一个阶段能替它收尾，所以只有这一处「强制中断」是划算的。
        """

        self.model_calls += 1
        raw = await asyncio.wait_for(
            provider.chat_structured(AnswerOut, system=FINALIZER_SYSTEM, user=prompt),
            timeout=FINALIZER_TIMEOUT_SECONDS,
        )
        try:
            return finalize_answer(raw, materials=self.materials)
        except AnswerRejected as exc:
            if exc.reason in _STRUCTURAL_REJECTIONS:
                # 结构不对 = 这一次没答出来（模型可能返回了别的东西）：值一次换模型重问。
                raise _AttemptFailed(exc.reason) from exc
            raise

    # ── 阶段间检查 ──

    def _budget_stop(self) -> StopReason | None:
        """进入新阶段前的唯一闸门：先看时间，再看模型调用次数。

        顺序固定（时间优先）：两个都越界时，报「慢」比报「调用次数多」更贴近调用方的
        感受，而且 ``timeout`` 还能解释「为什么这次明明没到 3 次调用也停了」。
        """

        if self._elapsed() >= STAGE_BUDGET_SECONDS:
            return StopReason.TIMEOUT
        if self.model_calls >= MAX_MODEL_CALLS:
            return StopReason.BUDGET_EXHAUSTED
        return None

    def _elapsed(self) -> float:
        return self.clock() - self._started

    # ── 模型调用与收尾 ──

    async def _call(self, schema: type[BaseModel], system: str, user: str) -> Any:
        """一次结构化调用；**先计数再发**——已经发出的调用就算失败也计成本。"""

        if self._provider is None:
            self._provider = await self.llm_factory()
        self.model_calls += 1
        return await self._provider.chat_structured(schema, system=system, user=user)

    def _outcome(self, reason: StopReason) -> ResearchOutcome:
        outcome = ResearchOutcome(
            points=tuple(self.points),
            queries=tuple(self.queries),
            materials=tuple(self.materials),
            searches=self.searches,
            followup_rounds=self.followup_rounds,
            model_calls=self.model_calls,
            duration_ms=int(self._elapsed() * 1000),
            stop_reason=reason,
            answer=self.answer,
            citations=self.citations,
            evidence_state=self.evidence_state,
            strong_model_fallback=self.strong_model_fallback,
            sub_search_degraded=_dedupe(self._sub_search_degraded),
            answer_rejected_reason=self.answer_rejected_reason,
        )
        # 响应与日志**同一套词**（父 spec：七种停止原因「响应 / 日志 / 界面同一套词」）。
        # 日志只放派生量与计数，绝不放原问题、查询原文或模型写的文本；拒绝原因是一个
        # 固定串（一因一码），所以它可以进日志。
        logger.info(
            "research finished",
            tenant=tenant_fingerprint(self.tenant_id),
            stop_reason=reason.value,
            model_calls=self.model_calls,
            searches=self.searches,
            followup_rounds=self.followup_rounds,
            points=len(self.points),
            materials=len(self.materials),
            duration_ms=outcome.duration_ms,
            evidence_state=outcome.evidence_state,
            answered=outcome.answer is not None,
            answer_rejected=self.answer_rejected_reason,
            strong_model_fallback=self.strong_model_fallback,
            sub_search_degraded=list(outcome.sub_search_degraded),
        )
        return outcome


def _dedupe(reasons: Sequence[str]) -> tuple[str, ...]:
    """去重保序：同一个原因出现两次不给任何信息，只会让那一栏随子查询条数膨胀。

    空串丢弃：注入的检索实现不该给空原因，但「给了一条空原因」不该变成响应里的一格空白
    ——那一栏的读者是拿它做统计的。
    """

    seen: dict[str, None] = {}
    for reason in reasons:
        if isinstance(reason, str) and reason.strip():
            seen.setdefault(reason, None)
    return tuple(seen)


async def run_research(
    *,
    query: str,
    tenant_id: str,
    search: SearchCallable,
    llm_factory: LLMFactory | None = None,
    strong_llm_factory: LLMFactory | None = None,
    sub_search_degraded: Sequence[str] = (),
    clock: Clock | None = None,
) -> ResearchOutcome:
    """跑一次「规划 → 并行检索 → 判断 → 补证 → 生成引用块」。

    ``search`` 是**注入的检索内核**（一次子查询一次调用）；``llm_factory`` 是规划 / 判断
    的模型工厂，``strong_llm_factory`` 是生成的模型工厂（缺省走 ``strong`` 角色）；
    ``clock`` 是可推进的单调时钟（缺省 ``time.monotonic``，用例用它把阶段预算推到 90 秒
    之外而不用真的等）。这些都是父 spec #138 定下的接缝，本函数不自己去造任何一个。

    ``sub_search_degraded`` 是**检索实现自己交上来的原因**（内核每降级一条记一条）：它必须
    从检索那一层带过来，因为「查的时候降级了」在材料里看不出来——而它又不能让子检索各写
    一行归属索引（ADR-0028）。于是外层收成一个列表递进来，这里只负责去重与上交。
    """

    run = _Run(
        query=query,
        tenant_id=tenant_id,
        search=search,
        llm_factory=llm_factory,
        strong_llm_factory=strong_llm_factory,
        sub_search_degraded=sub_search_degraded,
        clock=clock or time.monotonic,
    )
    return await run.execute()


__all__ = [
    "FollowupPlan",
    "MAX_DECIDER_MATERIAL_CHARS",
    "PlanPoint",
    "PlanQuery",
    "PlannedQuery",
    "ResearchOutcome",
    "ResearchPlan",
    "STRONG_MODEL_FAILED",
    "STRONG_MODEL_UNAVAILABLE",
    "build_decider_prompt",
    "build_planner_prompt",
    "material_key",
    "run_research",
]
