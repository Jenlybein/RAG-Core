"""``POST /api/v1/rag/research`` 的请求 / 响应模型（第 11 步票 #148）。

**请求体只有 ``kb_ids`` + ``query``**，与 ``/rag/retrieve`` 逐字段相同：Research 不接受
档位、条数、轮次、要点数等任何参数（ADR-0026 §1、ADR-0018「请求不进质量参数」）。所以
请求模型直接**继承**检索那个值对象，而不是照抄一份——照抄的那一份迟早与检索侧漂移，而
漂移的形态正是「同一个字段，两扇门两种答案」。

**响应里没有内部候选池**：材料是交付单元（节）的并集，轮次只给计数。计划里每条查询都
绑定一个要点序号，于是「这几次检索是为了回答哪一块」是结构性的，不需要调用方去猜。

``answer`` / ``citations`` 是**最终回答位**：本票它们恒为 ``null`` / ``[]``（#149 落地），
位置先留着——这样下一票补上内容时，响应形状不会跟着再变一次。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.rag import RetrieveRequest, RetrievedChunk


class ResearchRequest(RetrieveRequest):
    """一次 Research 的调用参数：与检索同一份值对象（``extra="forbid"`` 也一并继承）。

    多给的字段（档位、条数、轮次）拿到的是参数校验失败（``20002``），**不是被静默忽略**：
    静默忽略会让调用方以为「我指定了 2 轮」，实际跑的是固定五步。
    """


class ResearchPoint(BaseModel):
    """一个要点：``A1`` 起编号 + 一句话。它由规划器给，**不是**答案正文。"""

    point_id: str
    text: str


class ResearchQuery(BaseModel):
    """计划里的一条查询：绑定要点 + 轮次（1 首轮 / 2 补证）。

    只给轮次编号，不给「这一轮内部还有几条」这样的分组结构——那是内部候选池的形状。
    """

    point_id: str
    query: str
    round: int


class ResearchPlan(BaseModel):
    """查询计划：要点清单 + 查询清单（每条查询绑定一个要点）。"""

    points: list[ResearchPoint] = Field(default_factory=list)
    queries: list[ResearchQuery] = Field(default_factory=list)


class ResearchRounds(BaseModel):
    """轮次计数：一共跑了几次子检索、用掉几轮补证。**只给计数。**"""

    searches: int
    followup: int


class ResearchMetadata(BaseModel):
    """这次调用的元数据：计划名、耗时、模型调用次数、**一个**停止原因。

    ``stop_reason`` 就是 ``app.services.research_limits.StopReason`` 的那七个词之一：
    响应、日志、统计读同一套词（父 spec #138 C 段）。把成本写在这里是 ADR-0026 §5 的要求
    ——「每次 Research 花多久、几次模型调用」必须可读，否则它会被当成普通检索来用。
    """

    plan_name: str
    duration_ms: int
    model_calls: int
    stop_reason: str


class ResearchCitation(BaseModel):
    """一条引用块：文本 + 证据下标 + 状态（形状取自 ADR-0026 §2）。

    **本票不产出**（``citations`` 恒为空）：Finalizer 与四条硬校验是 #149 的事。先把形状
    定下来，是因为它是响应契约的一部分——下一票只填内容，不改外壳。
    """

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["supported", "insufficient"]


class ResearchData(BaseModel):
    """一次 Research 的响应数据。

    ``materials`` 复用检索的 :class:`~app.schemas.rag.RetrievedChunk`：材料就是交付单元
    （一节一条），同一个值对象让「材料的形状」不可能与 ``/rag/retrieve`` 漂移。
    """

    query: str
    kb_ids: list[str]
    # 最终回答位（#149）：本票恒为 None / 空列表。
    answer: str | None = None
    citations: list[ResearchCitation] = Field(default_factory=list)
    materials: list[RetrievedChunk] = Field(default_factory=list)
    plan: ResearchPlan
    rounds: ResearchRounds
    metadata: ResearchMetadata


def payload_of(
    *,
    query: str,
    kb_ids: list[str],
    outcome: Any,
    plan_name: str,
) -> dict[str, Any]:
    """把状态机的产物装成响应形状（唯一一处装填，HTTP 与将来的 MCP 共用）。

    ``outcome`` 是 :class:`app.services.research.ResearchOutcome`：这里只做「换外壳」，
    不重算任何判定——材料、计划、计数、停止原因全部原样搬过来。判定散在两处，就会出现
    「响应说两轮、日志说一轮」这种无法自证的错误。
    """

    return {
        "query": query,
        "kb_ids": list(kb_ids),
        "answer": outcome.answer,
        "citations": list(outcome.citations),
        "materials": list(outcome.materials),
        "plan": {
            "points": [
                {"point_id": point.point_id, "text": point.text} for point in outcome.points
            ],
            "queries": [
                {"point_id": item.point_id, "query": item.query, "round": item.round}
                for item in outcome.queries
            ],
        },
        "rounds": {
            "searches": outcome.searches,
            "followup": outcome.followup_rounds,
        },
        "metadata": {
            "plan_name": plan_name,
            "duration_ms": outcome.duration_ms,
            "model_calls": outcome.model_calls,
            "stop_reason": outcome.stop_reason.value,
        },
    }


__all__ = [
    "ResearchCitation",
    "ResearchData",
    "ResearchMetadata",
    "ResearchPlan",
    "ResearchPoint",
    "ResearchQuery",
    "ResearchRequest",
    "ResearchRounds",
    "payload_of",
]
