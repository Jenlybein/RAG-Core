"""受控 Research 的六条硬上限与七种停止原因（第 11 步票 #148）。

这两样东西**写死在代码里**（父 spec #138 C 段：「硬上限（常量，不走环境变量）」），理由
与切片编排的池子配额一字不差：它们是**成本契约**，不是部署偏好。做成可调项之后，同一个
``research_fixed`` 计划在两台机器上一次调用会烧掉不同数量的模型调用与子检索——调用方看到
的计划名一样，账单与延迟却不一样，而这两件事都没有第二处可以归因。

六条上限各自管什么、为什么是那个数：

| 常量 | 值 | 管什么 | 为什么是这个数 |
| --- | --- | --- | --- |
| :data:`MAX_POINTS` | 5 | 一次 Research 能拆出几个要点 | 再多就不是「一个问题」，而是一份问卷；要点进判断提示词，越少越准 |
| :data:`MAX_FIRST_ROUND_QUERIES` | 3 | 首轮并行子检索条数 | 一轮问答里真正互相独立的子问题很少超过 3 个；3 条也压住了首轮延迟 |
| :data:`MAX_FOLLOWUP_QUERIES` | 2 | 补证轮的子检索条数 | 补证是「差一块」的定向补，2 条以上说明首轮规划本身没做对 |
| :data:`MAX_FOLLOWUP_ROUNDS` | 1 | 补证跑几轮 | 一轮补不上就承认「材料不足」（ADR-0026 的 ``insufficient``），而不是无限追 |
| :data:`MAX_MODEL_CALLS` | 3 | 整次调用的模型调用次数 | 规划 / 判断 / 生成各一次，正好三份；第 4 次说明某一步在做重复劳动 |
| :data:`STAGE_BUDGET_SECONDS` | 90 | 整次调用的阶段预算 | 约 30 秒是典型值，90 秒是「明显不正常」的那条线 |

**阶段预算是「阶段间检查」，不是硬超时**：它在**进入新阶段之前**判一次，绝不强制中断
已经发出的单次外部请求（一次子检索、一次模型调用）。理由很具体——强制中断要取消一个
正在 ``await`` 的网络调用，而它已经付过钱、可能只差 200 毫秒就回来；把它丢掉换一条
``timeout`` 记录，等于花钱买一次失败。真正需要防的是「一个阶段接一个阶段地磨下去」，
那只需要在阶段边界上看一眼表。

**生成阶段是唯一的例外，而且只对「最后一次调用」例外**（票 #149）：它是状态机的最后一步，
后面没有下一个阶段能替它收尾——一次挂住的生成调用会把整个请求挂到自己超时为止。所以那里
有一个**单次调用的硬超时**（:data:`FINALIZER_TIMEOUT_SECONDS`），并配一个含兜底位的调用
上限（:data:`MAX_MODEL_CALLS_WITH_FALLBACK`）：强模型失败之后换快模型重试一次，总计最多
4 次，第 5 次不可能发生。

七种停止原因（:class:`StopReason`）是**响应与日志共用的同一套词**：调用方拿它做统计与
告警，运维拿它做排障，两边读的必须是同一份枚举——各写一份字符串的下场是某一天
「``timeout``」与「``budget_exhausted``」在某处被拼成了第三种写法，而没有任何测试会红。
"""

from __future__ import annotations

from enum import Enum
from typing import Final

#: 响应里的计划名（父 spec：「元数据含计划名 ``research_fixed``」）。
#:
#: 名字是**契约的一部分**：它说的是「这一次用的是固定五步、不是自由循环」，将来若有第二条
#: 规划策略（例如按要点数量自适应补证），它是调用方唯一能区分两者的字段。
PLAN_NAME: Final[str] = "research_fixed"

#: 一次 Research 最多拆出几个要点。
MAX_POINTS: Final[int] = 5

#: 首轮并行子检索的条数上限。
MAX_FIRST_ROUND_QUERIES: Final[int] = 3

#: 补证轮的子检索条数上限。
MAX_FOLLOWUP_QUERIES: Final[int] = 2

#: 补证最多跑几轮（本票固定 1：固定五步状态机，不是自由循环）。
MAX_FOLLOWUP_ROUNDS: Final[int] = 1

#: 整次调用的模型调用次数上限（规划 / 判断 / 生成各一次）。
MAX_MODEL_CALLS: Final[int] = 3

#: **含兜底位**的模型调用硬顶（票 #149）：三个阶段的 3 次 + 生成阶段换快模型重试的 1 次。
#:
#: 为什么兜底要单独占一格而不是挤进 :data:`MAX_MODEL_CALLS`：强模型的失败**已经花掉了**
#: 那一次调用（指标要如实反映「发出去过」），而「换了模型再答一次」是另一个决定。两者共用
#: 一个数字，就一定会在某一条路径上变成「要么兜底永远不生效，要么上限被悄悄突破」。
#: 4 是这条链路真正可能出现的最大次数，第 5 次在任何路径上都不成立。
MAX_MODEL_CALLS_WITH_FALLBACK: Final[int] = MAX_MODEL_CALLS + 1

#: 生成阶段**单次调用**的硬超时（秒）。它是状态机里唯一的硬超时，理由见模块文档：
#: 生成是最后一步，后面没有下一个阶段能替它收尾。超时按「这次没答出来」处理——
#: 换快模型兜底一次，仍不行就 ``answer = None``。
FINALIZER_TIMEOUT_SECONDS: Final[float] = 30.0

#: 整次调用的阶段预算（秒）。**阶段间检查**，不中断已发出的单次请求。
STAGE_BUDGET_SECONDS: Final[int] = 90


class StopReason(str, Enum):
    """七种固定停止原因：``str`` 混入是为了让它能直接进响应 JSON，不必再翻译一次。

    每个成员的语义是「**为什么停在这里**」，不是「结果好不好」：

    * :attr:`EVIDENCE_COMPLETE`：判断阶段认为所有要点都有材料直接回答，不需要补证；
    * :attr:`NO_NEW_EVIDENCE`：补证跑过了，但没有捞到任何一条新材料；
    * :attr:`MAX_ROUNDS`：补证跑过了且捞到了新材料，但轮次已经用尽（不再判断、不再补）；
    * :attr:`DUPLICATE_QUERY`：判断阶段给的查询全都已经跑过，一条都不该重跑；
    * :attr:`TIMEOUT`：阶段预算用尽，没进入下一个阶段（已到手的材料照给）；
    * :attr:`BUDGET_EXHAUSTED`：模型调用次数用尽，同上；
    * :attr:`DEGRADED`：规划或判断失败，按降级出口收尾（材料照给）。
    """

    EVIDENCE_COMPLETE = "evidence_complete"
    NO_NEW_EVIDENCE = "no_new_evidence"
    MAX_ROUNDS = "max_rounds"
    DUPLICATE_QUERY = "duplicate_query"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEGRADED = "degraded"


__all__ = [
    "FINALIZER_TIMEOUT_SECONDS",
    "MAX_FOLLOWUP_QUERIES",
    "MAX_FOLLOWUP_ROUNDS",
    "MAX_FIRST_ROUND_QUERIES",
    "MAX_MODEL_CALLS",
    "MAX_MODEL_CALLS_WITH_FALLBACK",
    "MAX_POINTS",
    "PLAN_NAME",
    "STAGE_BUDGET_SECONDS",
    "StopReason",
]
