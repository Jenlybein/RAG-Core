"""受控 Research：固定五步状态机（第 11 步票 #148 / #149；收窄见票 #164）。

```text
① 规划        问题（**只有问题**）→ ≤5 个要点 + 每个要点各一条首轮查询（≤3 条被执行）
      │ 失败 → 退回原问题跑一次普通检索，code 0，停止原因 degraded
② 并行检索    每条查询各跑一次「检索内核」（asyncio.gather）→ 材料池 pool0
      │
③ 覆盖判定    逐要点问「材料里有没有**直接读出**它的事实」→ supported(材料序号) / none
      │ 失败 → 停止补证，材料照给，停止原因 degraded，coverage 全部 unknown
④ 定向补证    只对 none 的要点补检索（≤2 条，≤1 轮），并行执行、合并新材料
      │
⑤ 最终回答    最终材料池 → 引用块（只能点下标）→ 四条硬校验 → 任一不过 answer = null
      │ 有 gap 时答案必须声明「材料不足」，否则同样作废（缺口闸）
      │ 强模型未配 / 失败 / 超时 → 快模型兜底一次，并在 metadata 标记（不报错）
```

**基准在问题侧，不在材料侧**（ADR-0029 决策三，本票的核心）。要点由「只看得见问题的」那次
调用产出，所以**材料里没有的东西不会因为归纳不出来而消失**——它会落在某个要点上成为
``none``，再成为 ``coverage.gaps`` 里的一条。反过来做（要点从材料里归纳）会让「材料里没有
的东西」在输出空间里**根本没有位置**：材料里没有 → 归纳不出来 → 不会进要点 → 不会缺核心片
→ 「齐了」恒为真。``tests/test_research_coverage.py`` 用三条守卫钉住这条边界。

**定义闸：「支撑」= 材料里能直接读出该事实，不是「相关」。** 这条定义同时写在提示词
（:data:`_JUDGE_SYSTEM`）与本模块文档里：不写死，模型会把「主题相关」当成「支撑」，
覆盖率立刻变乐观，而乐观的覆盖率比没有覆盖率更危险——它会替一份缺口答案盖章。

**``none`` 是一等输出，``unknown`` 是它的诚实邻居**：判定没跑 / 判不了时，``coverage`` 里
每条都是 ``unknown``（不知道），而不是 ``none``（知道没有）；补证捞到了新材料但**没有再审
一次**的要点同样是 ``unknown``——状态机固定五步、不回头复判，于是「材料到了但支撑未经判定」
只能如实记成不知道。``unknown`` 与 ``none`` 混成一个词，会让缺口闸在「其实有材料」的题上
把答案判死；反过来把 ``none`` 记成 ``unknown``，缺口就永远不会被声明。

**这是状态机，不是自由循环**：五步按固定顺序各走一次，补证跑完就结束。于是「最慢多久、
最多几次模型调用、最多几次检索」全部可以从 :mod:`app.services.research_limits` 的常量
推出来。反过来做（「不够就再判断一次」）会让延迟与成本取决于模型这一次的心情，而父 spec
#138 把「延迟与成本可预估」写成了验收项。

**它吃「注入的检索内核 + 模型工厂」两个输入**（ADR-0028 §4）：检索内核是
``Callable[[str], Awaitable[...]]``——一次子查询一次调用；模型工厂与切片编排同形
（``get_llm_provider("fast")``）。于是五步的每条降级出口、六条硬上限与七种停止原因都能
脱开真实检索与真实模型被断言（``tests/test_research_state_machine.py``）。

**⑤ 的门槛在另一个模块**（票 #149）：块的结构、四条硬校验、渲染串、三态完整度与缺口闸全在
:mod:`app.services.citation_blocks`，本模块只负责「什么时候问、问哪一份材料、哪几个要点是
缺口、拿到不合法的东西怎么退」。生成用**强模型角色**：它要准，不要快；未配置 / 失败 / 超时
换快模型兜底一次，标记进 ``metadata.strong_model_fallback``——那是成本与质量之间一次
**看得见**的交换，不是一次静默降级。

**子查询不进改写、不写归属索引、不记账、不发观测**：这四件事一件都不在本模块里，也不在
它调的内核里（ADR-0028 §2 的五件事全在外层，由 HTTP 入口做一次）。判据只有一句——「每条
子查询都要各做一次，还是每次调用只应做一次」。一次 Research 的身份是一行归属索引、一份
trace，子检索在它里面以阶段出现；归属索引那一行因此写在外层（``research_gate``），本模块
只把「子检索自己降级了哪几条原因」原样收上来（``sub_search_degraded``），供外层与响应共用。

**材料只有一个来源：内核交回的交付单元**。模型只写「要点」「查询」「逐要点的判定」与
「块的一句话」四种文本，它返回的任何正文一律丢弃（材料正文逐字来自库，ADR-0026 §4）。

**任何阶段都不许让整单报错**：规划失败、判定失败、生成失败都收敛成「材料照给 + 一条停止
原因（+ 一条可读的元数据）」。但是**子检索失败不在此列**——它意味着这套集成的检索本身
不可用（关键词引擎没配、库不可见、嵌入服务挂了），如实上抛比包成一份「看起来成功、材料
是空的」响应诚实得多。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

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
    evidence_id,
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

#: 逐要点覆盖判定的三个词（**一处定义**，响应里的 ``coverage.points[].status`` 取它们）：
#:
#: * :data:`SUPPORT_SUPPORTED`：材料里能**直接读出**该要点的事实（判定给材料序号）；
#: * :data:`SUPPORT_NONE`：材料里没有能直接读出它的事实——**缺口**，会进 ``coverage.gaps``；
#: * :data:`SUPPORT_UNKNOWN`：**不知道**（判定没跑、判定失败，或补证捞到新材料但没有再审
#:   一次）。它与 ``none`` 是两件事：``none`` 是「知道没有」，``unknown`` 是「不敢说没有」。
#:
#: 三个词里 ``none`` 是这一轮改造的全部意义（ADR-0029 决策三）：「材料里没有」必须是一个
#: **可表达**的输出，否则它只能靠「没被归纳进要点」来隐形，而隐形等于不存在。
SUPPORT_SUPPORTED: Final[str] = "supported"
SUPPORT_NONE: Final[str] = "none"
SUPPORT_UNKNOWN: Final[str] = "unknown"

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


class PointSupportOut(BaseModel):
    """一个要点的覆盖判定，**模型能表达的全部**：要点序号 + 状态 + 支撑它的材料序号。

    状态只有两个（``supported`` / ``none``）：要模型「有就说有、没有就说没有」，不许它拿
    一个「部分支持」逃出去——那一档既不能当支撑用（会变乐观），也不该算缺口（会变悲观）。
    ``unknown`` 不是模型能写的东西，它是**状态机**在判定没跑成时给每条要点盖的章。
    """

    point: int
    status: Literal["supported", "none"]
    materials: list[int] = Field(default_factory=list)


class CoverageJudgement(BaseModel):
    """③ 覆盖判定的结构化输出：逐要点结论 + 只针对 ``none`` 的补证查询。

    两个字段的分工是固定的，**结论是唯一事实来源**：``points`` 说「这个要点有没有支撑」，
    ``queries`` 只说「为了那个没有支撑的要点，我想换一种说法再查一次」。于是旧设计里那句
    「两份输出可能互相矛盾」有了裁决处——补证查询绑到已判为 supported 的要点上时，那条查询
    直接丢掉（:func:`_targeted_followup`），结论不受影响。

    这也解释了为什么 ``none`` 必须是一个显式状态而不是「没有给查询」：没有查询只说明模型
    想不出更好的说法，不说明材料里没有；反之材料里没有的时候，它完全可能还提出一条查询。
    两件事各自有各自的字段，谁也不替谁说话。
    """

    points: list[PointSupportOut] = Field(default_factory=list)
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
class PointVerdict:
    """一个要点的覆盖判定（**已收敛、已校验**）：要点编号 + 状态 + 它引到的证据编号。

    ``status`` 取 :data:`SUPPORT_SUPPORTED` / :data:`SUPPORT_NONE` / :data:`SUPPORT_UNKNOWN`。
    前两个来自模型（模型只写这两个），第三个由状态机盖：判定没跑成、或补证捞到了新材料却
    没有再审一次。

    ``evidence_ids``（``E1`` 起）与引用块用的是同一套编号、同一个材料池，于是「这个要点靠
    哪几条材料站着」在响应里直接可读——那是 ``coverage`` 作为「答案为什么不完整」的解释，
    而不是一份新的自评。
    """

    point_id: str
    status: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchOutcome:
    """一次 Research 交回的东西：计划、材料、轮次计数、元数据、**一个**停止原因与最终回答。

    ``answer`` / ``citations`` 是**最终回答位**（票 #149）：``answer`` 是引用块的渲染串，
    ``citations`` 是被四条硬校验放行的那几块。任何一块不过校验 → 两者都回到 ``None`` / 空，
    材料与计划照给（ADR-0026 §3）。

    四个「不丢信息」的字段各有分工，谁也不许吞掉谁：

    * ``coverage``：**逐要点**的覆盖判定（票 #164），``gaps`` 是它里面状态为 ``none`` 的那些
      要点编号。它回答的是「材料里有没有支撑 P_i」，与 :attr:`evidence_state`（从核心片数
      算出来的三态完整度）**不是同一件事**：前者以**问题侧**的要点为基准，后者以材料侧的核心
      片为基准。两个都要有，因为它们能分开说「材料不够」与「答得不够」；
    * ``evidence_state``：三态完整度（``complete`` / ``partial`` / ``missing``），由核心片
      数算出来——「材料够不够」不能被 ``stop_reason``（那说的是**为什么停**）吞掉；
    * ``strong_model_fallback``：这次生成是不是快模型兜的底，以及为什么（两个固定串之一）；
    * ``sub_search_degraded``：子检索自身降级的去重列表（一因一码，原样上交给外层写行）。

    ``answer_rejected_reason`` 是**内部**字段（不进响应）：它是四条硬校验与缺口闸的拒绝码，
    用于日志与用例断言「哪一种输入被哪一条挡住了」。响应里只有 ``answer = null``——调用方要
    的是「这次不生成」，不是「后端替你判了哪一条」。
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
    coverage: tuple[PointVerdict, ...] = ()

    @property
    def gaps(self) -> tuple[str, ...]:
        """缺口 = 判定为 ``none`` 的那些要点编号。**唯一一处推导**（响应也读它）。

        ``unknown`` 不进 gaps：不知道的时候既不该声明「材料不足」，也不该假装有支撑。
        """

        return tuple(item.point_id for item in self.coverage if item.status == SUPPORT_NONE)


# ─────────────────────────────────────────────────────────── 提示词


_PLANNER_SYSTEM = (
    "你在为一次知识库检索做计划。给定用户的问题：第一，列出回答它需要的要点，"
    f"每个要点一句不超过 30 字的中文，最多 {MAX_POINTS} 个；第二，为**每一个**要点各给一条"
    f"可以直接拿去检索的查询（一个要点一条，一条都不能少），每条查询绑定它对应的要点序号"
    f"（从 1 起）；首轮最多执行 {MAX_FIRST_ROUND_QUERIES} 条查询，所以**宁可少列要点，也不要"
    "列出一个没有查询的要点**。要求：只做检索计划，不要生成答案正文，不要复述材料，不要输出"
    "切片 id 或文档 id；查询写成文档里可能出现的陈述句，不要写成向助手提的问句。"
)

_JUDGE_SYSTEM = (
    "你在判定一次检索的材料能不能支撑回答。**「支撑」的定义：材料里能直接读出该事实**，"
    "不是「主题相关」——相关但读不出结论的材料不算支撑。"
    "给定问题、要点清单（A1 起）与已经检索到的材料（带序号），"
    "对**每一个要点**各给一条判定：status=supported 并列出能直接读出该事实的材料序号；"
    f"或 status={SUPPORT_NONE}（材料里没有能直接读出它的事实）——没有支撑时必须写 {SUPPORT_NONE}，"
    "不要为了凑出一个完整答案把它算成 supported，也不要因为「看起来相关」就当成支撑。"
    f"对 status 为 {SUPPORT_NONE} 的要点，各给一条换过说法、可以直接拿去检索的定向补证查询，"
    f"最多 {MAX_FOLLOWUP_QUERIES} 条。要求：每个要点都要有一条判定，一个都不许漏；"
    "只输出要点序号、状态、材料序号与查询文本；不要输出材料正文，不要输出切片 id，"
    "不要生成答案。"
)


def build_planner_prompt(*, query: str) -> str:
    """规划阶段的用户消息：**只有原问题**。

    不给它任何材料是刻意的：规划发生在检索之前，此时手里没有任何材料；把「以往的结果」
    塞进来会让它按上一轮的记忆列要点（同一句话两次调用得到两份不一样的计划，而这次没有
    任何东西变了）。
    """

    return f"问题：{query}"


def build_judge_prompt(
    *, query: str, points: Sequence[PlanPoint], materials: Sequence[Mapping[str, Any]]
) -> str:
    """覆盖判定的用户消息：问题 + 要点清单 + 已到手的材料（带序号、带预算）。

    材料带序号而不带 id：模型能表达的只有「第几条撑住了第几个要点」，不许它指向某一条具体
    材料的身份（那会变成一次隐式的角色分配——那是切片编排的活，不是这里）。正文仍然按
    :data:`MAX_DECIDER_MATERIAL_CHARS` 截断：截断只影响这一次提示词，不影响交回给调用方的
    材料（「成本可以截断，材料不许截断」）。
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


def _require_every_point_queried(
    pairs: Sequence[tuple[int, str]], *, points: Sequence[PlanPoint]
) -> None:
    """**每个要点都必须有一条绑定它的检索查询**，少一条这份计划就不成立。

    为什么这是硬性失败，而不是「补一条空查询」或「把这个要点去掉」：

    * 补一条空查询：那一次检索等于没查，而覆盖判定会把「没查过」判成「材料里没有」——
      凭空造出一个假缺口；
    * 去掉这个要点：缺口根本不会出现——那正是 ADR-0029 决策三点名的盲区（输出空间里没有
      「没有」）；要点是从**问题**里分解出来的，不许被材料侧的可行性倒删。

    所以一个没配上查询的要点只有一种诚实处置：承认这份计划没成立，退回原问题跑一次普通
    检索（规划失败的那条降级出口）。上限截断（:data:`MAX_FIRST_ROUND_QUERIES`）在**这条
    检查之后**发生：截断影响的是「执行几条」，不是「计划里有没有」。
    """

    queried = {point for point, _text in pairs}
    if any(index not in queried for index in range(1, len(points) + 1)):
        raise _IllegalOutput("a point without a retrieval query")


def _coerce_plan(raw: Any) -> tuple[list[PlanPoint], list[PlanQuery]]:
    """规划输出 → (要点清单, 首轮查询)。任一处不合法都抛 :class:`_IllegalOutput`。"""

    texts = _coerce_points(raw)
    points = [
        PlanPoint(point_id=f"A{index}", text=text) for index, text in enumerate(texts, start=1)
    ]
    pairs = _coerce_query_items(raw)
    _require_every_point_queried(pairs, points=points)
    queries = _bound_queries(pairs, points=points, round_no=1)
    if not queries:
        # 要点有了、查询一条都没有：这不构成一次检索计划（没法检索）。按规划失败处理，
        # 退回原问题跑一次普通检索——比交回一份「有要点、没材料」的材料诚实。
        raise _IllegalOutput("a plan without queries")
    return points, queries


def _coerce_judged_queries(raw: Any) -> list[tuple[int, str]]:
    """判定里的补证查询：**字段缺省等于「没有补证查询」**（不是「模型没答」）。

    与规划阶段相反：规划没给查询 = 计划不成立（一份没法执行的计划），而判定没给查询只是
    「我想不出更好的说法」——结论仍然有效，缺口照样成立。两者共用一个 ``_coerce_query_items``
    会把这个区别抹掉，所以这条分叉写在这里而不是写在那个函数里。
    """

    if _field(raw, "queries") is None:
        return []
    return _coerce_query_items(raw)


def _material_orders(value: Any) -> list[int]:
    """判定里的材料序号列表：只认真正的整数（``bool`` 是 ``int`` 的子类，得单独挡掉）。"""

    if value is None:
        return []
    if not isinstance(value, list):
        raise _IllegalOutput("materials is not a list")
    orders: list[int] = []
    for item in value:
        order = _as_int(item)
        if order is None:
            raise _IllegalOutput("a material order is not an integer")
        orders.append(order)
    return orders


def _coerce_coverage(
    raw: Any, *, points: Sequence[PlanPoint], material_count: int
) -> list[PointVerdict]:
    """覆盖判定输出 → 逐要点结论（按要点顺序）。不合法就抛 :class:`_IllegalOutput`。

    五条判定写在一处，因为它们必须同时成立（任何一条松掉，``coverage`` 就不再可信）：

    1. **每个要点都要有一条结论**。漏答不能猜成 ``supported``（乐观），也不能猜成 ``none``
       （凭空造缺口）——两种猜测都在替模型回答它没回答的问题；
    2. **一个要点只能有一条结论**：两条结论时不知道该信哪一条；
    3. **序号必须在范围内**：要点序号对着计划清单，材料序号对着**判定当时**的材料池（材料池
       只追加，所以这些序号在补证之后仍然指向同一条材料，可以直接当证据编号用）；
    4. **状态必须自证**：``supported`` 必须给材料序号（「有支撑」要有指向），``none`` 必须一条
       都不给（「没有支撑」不许挂着材料）；
    5. 结论按要点顺序返回——响应里的 ``coverage.points`` 因此与 ``plan.points`` 同序，
       调用方不必自己排序。
    """

    items = _field(raw, "points")
    if items is None:
        raise _IllegalOutput("no coverage verdicts")
    if not isinstance(items, list):
        raise _IllegalOutput("coverage verdicts is not a list")

    seen: dict[int, PointVerdict] = {}
    for item in items:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            raise _IllegalOutput("a verdict is not an object")
        order = _as_int(item.get("point"))
        if order is None or not 1 <= order <= len(points):
            raise _IllegalOutput("a verdict points at a point that does not exist")
        if order in seen:
            raise _IllegalOutput("a point has two verdicts")
        status = item.get("status")
        if status not in (SUPPORT_SUPPORTED, SUPPORT_NONE):
            raise _IllegalOutput("a verdict has an unknown status")
        orders = _material_orders(item.get("materials"))
        if any(not 1 <= index <= material_count for index in orders):
            raise _IllegalOutput("a verdict points beyond the material pool")
        if status == SUPPORT_SUPPORTED and not orders:
            raise _IllegalOutput("a supported verdict without materials")
        if status == SUPPORT_NONE and orders:
            raise _IllegalOutput("a none verdict carrying materials")
        seen[order] = PointVerdict(
            point_id=points[order - 1].point_id,
            status=status,
            evidence_ids=tuple(evidence_id(index) for index in sorted(set(orders))),
        )

    if len(seen) != len(points):
        raise _IllegalOutput("a point without a verdict")
    return [seen[index] for index in range(1, len(points) + 1)]


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
        self.coverage: list[PointVerdict] = []
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
        self._judged = False
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

        # 阶段间检查（覆盖判定前）：预算或模型调用用尽 → 不进新阶段，材料照给。
        gate = self._budget_stop()
        if gate is not None:
            return gate

        # ③ 覆盖判定（逐要点）。失败 → 降级，coverage 全部 unknown（不知道，不是没有）。
        judged = await self._judge()
        if judged is None:
            return StopReason.DEGRADED
        verdicts, proposed = judged
        self.coverage = list(verdicts)
        self._judged = True
        if not self._gap_verdicts():
            return StopReason.EVIDENCE_COMPLETE

        # ④ 定向补证：只对没有支撑的要点补检索。
        targeted = self._targeted_followup(proposed)
        fresh = self._fresh(targeted)[:MAX_FOLLOWUP_QUERIES]
        if not fresh:
            return StopReason.DUPLICATE_QUERY

        # 阶段间检查（补证前）。
        gate = self._budget_stop()
        if gate is not None:
            return gate

        # 补证（≤1 轮）。跑完就结束：捞到了材料 = max_rounds，白跑一趟 = no_new_evidence。
        added = await self._parallel(fresh, followup=True)
        self._settle_after_followup(fresh, added)
        return StopReason.MAX_ROUNDS if sum(added) else StopReason.NO_NEW_EVIDENCE

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

    async def _parallel(self, queries: Sequence[PlanQuery], *, followup: bool = False) -> list[int]:
        """并行跑一批查询，返回**每条查询各自新增**的材料条数（与入参同序）。

        ``asyncio.gather`` 的顺序与入参一致，合并也按这个顺序：同样的输入两次跑出同样的
        材料顺序（``E`` 编号将来要稳定，ADR-0026 §2）。

        为什么要按条分开记「新增几条」：补证之后要判「这一条补证有没有给那个缺口要点带回
        材料」，而「这一轮共新增 3 条」说不出是哪一条查询带回来的。
        """

        batches = await asyncio.gather(*(self._search_one(item.query) for item in queries))
        added = [self._merge(batch) for batch in batches]
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

    # ── ③ 覆盖判定 ──

    async def _judge(self) -> tuple[list[PointVerdict], list[PlanQuery]] | None:
        """逐要点判定「材料里有没有直接读出它的事实」。

        ``None`` = 判定失败（停止补证、标记降级、材料照给、``coverage`` 全部 ``unknown``）。
        失败出口照旧只有这一条：整份判定作废比采信半份预测好——半份判定里「没答的要点」
        既可能是漏了也可能是没有，而猜错任何一边都会污染 ``coverage``。
        """

        try:
            raw = await self._call(
                CoverageJudgement,
                _JUDGE_SYSTEM,
                build_judge_prompt(
                    query=self.query, points=self.points, materials=self.materials
                ),
            )
            verdicts = _coerce_coverage(
                raw, points=self.points, material_count=len(self.materials)
            )
            queries = _bound_queries(_coerce_judged_queries(raw), points=self.points, round_no=2)
        except Exception as exc:  # noqa: BLE001 - 判定失败即停止补证，绝不整单失败
            logger.warning(
                "research coverage judgement failed",
                tenant=tenant_fingerprint(self.tenant_id),
                stage="judge",
                error=type(exc).__name__,
            )
            return None
        return verdicts, queries

    def _gap_verdicts(self) -> list[PointVerdict]:
        """判定为 ``none`` 的那些要点——**缺口**的唯一来源（``unknown`` 不在其中）。"""

        return [item for item in self.coverage if item.status == SUPPORT_NONE]

    def _targeted_followup(self, proposed: Sequence[PlanQuery]) -> list[PlanQuery]:
        """补证查询：只留**绑在缺口要点上**的，缺口没给查询的拿要点文本顶上（定向补证）。

        两条规则各有理由：

        * **只留缺口要点上的**。结论是唯一事实来源：模型把一条查询绑到已判为 ``supported``
          的要点上时，那条查询丢掉（记一条 warning）——重跑一个已经有材料的要点，白花一次
          检索与一次嵌入，而且会让「补证只补没有支撑的那一块」这句话在行为上不成立；
        * **缺口要点必须有一条自己的查询**。模型想不出更好的说法时（它给的查询可能是空），
          就拿要点本身当查询：要点就是一句为这个问题写的话，直接拿去检索是合理的。这条让
          「每个缺口都定向补一次」不依赖模型的自觉，也不额外花模型调用。
        """

        gaps = self._gap_verdicts()
        gap_ids = {item.point_id for item in gaps}
        kept = [item for item in proposed if item.point_id in gap_ids]
        if len(kept) != len(proposed):
            logger.warning(
                "research followup retargeted",
                tenant=tenant_fingerprint(self.tenant_id),
                dropped=len(proposed) - len(kept),
            )

        seen = {_normalize(item.query) for item in kept}
        queried = {item.point_id for item in kept}
        texts = {point.point_id: point.text for point in self.points}
        for item in gaps:
            if item.point_id in queried:
                continue
            key = _normalize(texts[item.point_id])
            if key in seen:
                continue
            seen.add(key)
            kept.append(PlanQuery(point_id=item.point_id, query=texts[item.point_id], round=2))
        return kept

    def _settle_after_followup(
        self, queries: Sequence[PlanQuery], added: Sequence[int]
    ) -> None:
        """补证之后把缺口收成两种状态：仍然没有材料的（``none``）与材料到了的（``unknown``）。

        「材料到了」不等于「这个要点被支撑了」：状态机固定五步、**不回头复判**，所以补回来
        一条材料之后没有人再回答过「它支撑这个要点吗」。记成 ``supported`` 是乐观（ADR-0029
        反对的正是这种乐观），记成 ``none`` 则会让缺口闸把一份其实有材料的答案判死——只有
        ``unknown`` 说的是实话。
        """

        recovered = {
            item.point_id
            for item, count in zip(queries, added, strict=True)
            if count > 0
        }
        self.coverage = [
            PointVerdict(item.point_id, SUPPORT_UNKNOWN, item.evidence_ids)
            if item.status == SUPPORT_NONE and item.point_id in recovered
            else item
            for item in self.coverage
        ]

    def _fresh(self, queries: Sequence[PlanQuery]) -> list[PlanQuery]:
        """去掉已经跑过的查询（首轮 + 已发生的补证），一条都不重跑。"""

        done = {_normalize(item.query) for item in self.queries}
        return [item for item in queries if _normalize(item.query) not in done]

    # ── ⑤ 最终回答位（#149）──

    async def _finalize(self) -> None:
        """最终回答：只用最终材料池生成引用块，任一硬校验不过就 ``answer = None``。

        四件事按顺序做，顺序不能换：

        1. **没有材料就不问模型**（问了也只能编）：``answer = None``、``evidence_state``
           是 ``missing``——「根本没有材料」与「材料不足」在结构上要能分开。这时**不生成
           答案也就是最彻底的声明**：缺口由 ``coverage.gaps`` 逐要点交回，比在渲染串里追加
           一句没有块支撑的话诚实；
        2. **问强模型**（未配置 / 失败 / 超时换快模型兜底一次，标记进元数据）；
        3. **过四条硬校验**（:func:`finalize_answer`）：任何一块不过，整份答案作废，
           材料与计划照给——这是 ADR-0026 §3 的默认落点，不是一次失败；
        4. **过缺口闸**（票 #164）：判定说某个要点在材料里没有直接支撑时，答案里必须有
           一句「材料不足」——没有就是用自己的知识把缺口补上了，整份作废；
        5. **渲染串与块一起收下**：``answer`` 永远是块的渲染串，没有第二种来源。

        缺口要点（``self._gap_verdicts()``）在生成前就点名进提示词：指令与校验必须成对，
        只有校验没有指令等于让人猜一条不写在纸上的规矩。
        """

        if not self.materials:
            self.evidence_state = COMPLETENESS_MISSING
            return

        uncovered = self._uncovered_texts()
        prompt = build_finalizer_prompt(
            query=self.query, materials=self.materials, uncovered=uncovered
        )
        try:
            result = await self._answer_blocks(prompt, uncovered=uncovered)
        except AnswerRejected as exc:
            # 四条硬校验或缺口闸之一：**模型答了，但结论不能发**。不换模型重问（那是用成本
            # 换概率），整单不生成——材料、计划与缺口清单仍然完整交回。
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

    def _uncovered_texts(self) -> list[str]:
        """缺口的要点正文（按要点顺序）：提示词点名与缺口闸共用这一份。

        只取 ``none`` 的要点——``unknown`` 不进去：材料到了但没再审过的要点不该被当成缺口
        点名，否则模型会为一块其实有材料的要点写「材料不足」。
        """

        texts = {point.point_id: point.text for point in self.points}
        return [texts[item.point_id] for item in self._gap_verdicts()]

    async def _answer_blocks(self, prompt: str, *, uncovered: Sequence[str]) -> FinalAnswer:
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
            return await self._ask_once(await self.llm_factory(), prompt, uncovered=uncovered)

        try:
            return await self._ask_once(provider, prompt, uncovered=uncovered)
        except AnswerRejected:
            # 硬校验不过：这是「答了但不能发」，不是「没答出来」——不重试。
            raise
        except Exception:
            self.strong_model_fallback = STRONG_MODEL_FAILED
            if self.model_calls >= MAX_MODEL_CALLS_WITH_FALLBACK:
                raise
            return await self._ask_once(await self.llm_factory(), prompt, uncovered=uncovered)

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

    async def _ask_once(
        self, provider: Any, prompt: str, *, uncovered: Sequence[str]
    ) -> FinalAnswer:
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
            return finalize_answer(raw, materials=self.materials, uncovered=uncovered)
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

    def _coverage_for_outcome(self) -> tuple[PointVerdict, ...]:
        """交回给调用方的逐要点判定：判过就是判定本身，**没判过就全是 unknown**。

        「没判过」有几种来历（规划失败、阶段预算在判定之前用尽、判定失败），它们对一个
        调用方来说是同一件事：这次没有人看过「哪个要点有支撑」。这时候给 ``none`` 会凭空造
        缺口，给 ``supported`` 会假装有支撑，所以只有 ``unknown`` 是诚实的。
        """

        if self._judged:
            return tuple(self.coverage)
        return tuple(
            PointVerdict(point_id=point.point_id, status=SUPPORT_UNKNOWN)
            for point in self.points
        )

    def _outcome(self, reason: StopReason) -> ResearchOutcome:
        coverage = self._coverage_for_outcome()
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
            coverage=coverage,
        )
        # 响应与日志**同一套词**（父 spec：七种停止原因「响应 / 日志 / 界面同一套词」）。
        # 日志只放派生量与计数，绝不放原问题、查询原文或模型写的文本；拒绝原因是一个
        # 固定串（一因一码），所以它可以进日志。缺口只记条数：要点文本是模型写的，
        # 它不该进日志（那是一条可能夹带正文的通道）。
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
            coverage_gaps=len(outcome.gaps),
            answered=outcome.answer is not None,
            answer_rejected=self.answer_rejected_reason,
            strong_model_fallback=self.strong_model_fallback,
            sub_search_degraded=list(outcome.sub_search_degraded),
        )
        return outcome


def _dedupe(reasons: Sequence[str]) -> tuple[str, ...]:
    """去重保序：同一个原因出现两次不给任何信息，只会让那一栏随子查询条数膨胀。

    空串丢弃：注入的检索实现不该回一条空串，但「回了一条空串」不该变成响应里的一格空白
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
    """跑一次「规划 → 并行检索 → 覆盖判定 → 定向补证 → 生成引用块」。

    ``search`` 是**注入的检索内核**（一次子查询一次调用）；``llm_factory`` 是规划 / 覆盖判定
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
    "CoverageJudgement",
    "MAX_DECIDER_MATERIAL_CHARS",
    "PlanPoint",
    "PlanQuery",
    "PlannedQuery",
    "PointSupportOut",
    "PointVerdict",
    "ResearchOutcome",
    "ResearchPlan",
    "STRONG_MODEL_FAILED",
    "STRONG_MODEL_UNAVAILABLE",
    "SUPPORT_NONE",
    "SUPPORT_SUPPORTED",
    "SUPPORT_UNKNOWN",
    "build_judge_prompt",
    "build_planner_prompt",
    "material_key",
    "run_research",
]
