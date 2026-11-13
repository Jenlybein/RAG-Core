"""``POST /api/v1/rag/research`` 的请求 / 响应模型（第 11 步票 #148）。

**请求体只有 ``kb_ids`` + ``query``**，与 ``/rag/retrieve`` 逐字段相同：Research 不接受
档位、条数、轮次、要点数等任何参数（ADR-0026 §1、ADR-0018「请求不进质量参数」）。所以
请求模型直接**继承**检索那个值对象，而不是照抄一份——照抄的那一份迟早与检索侧漂移，而
漂移的形态正是「同一个字段，两扇门两种答案」。

**响应里没有内部候选池**：材料是交付单元（节）的并集，轮次只给计数。计划里每条查询都
绑定一个要点序号，于是「这几次检索是为了回答哪一块」是结构性的，不需要调用方去猜。

``answer`` / ``citations`` 是**最终回答**（票 #149 起真的会产出）：``answer`` 只是引用块的
渲染串，四类硬校验任一不过就回到 ``null`` / ``[]``，材料与计划照给（ADR-0026 §2 / §3）。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.exceptions import AppError, ErrorCode
from app.schemas.rag import RetrieveRequest, RetrievedChunk
from app.services.research_limits import StopReason


class ResearchRequest(RetrieveRequest):
    """一次 Research 的调用参数：与检索同一份值对象（``extra="forbid"`` 也一并继承）。

    多给的字段（档位、条数、轮次）拿到的是参数校验失败（``20002``），**不是被静默忽略**：
    静默忽略会让调用方以为「我指定了 2 轮」，实际跑的是固定五步。
    """


def parse_research_request(*, kb_ids: Any, query: Any) -> ResearchRequest:
    """两条入口共用的解析入口：校验规则只来自 :class:`ResearchRequest`（票 #151）。

    与 ``app.schemas.rag.parse_retrieve_request`` 逐字同形，理由也同一条：HTTP 侧由
    FastAPI 把同一个模型接在请求体上，MCP 的工具签名只有类型、没有 ``Field`` 约束，所以
    工具必须显式走一遍同一个值对象，再把 ``ValidationError`` 翻成同一个业务码（``20002``）。

    这里**不复制**字段规则，也不另写一份「Research 版的空值判断」：``ResearchRequest``
    继承 ``RetrieveRequest``，所以空列表、空串、纯空白、多给的字段（``rounds`` / ``top_k``）
    全部由同一处挡住。MCP 与 HTTP 因此不可能对同一份请求给出两种答案。
    """

    try:
        return ResearchRequest(kb_ids=kb_ids, query=query)
    except ValidationError as exc:
        # 与 HTTP 的 20002 同一个码、同一个缺省消息（``request validation error``）。
        raise AppError(ErrorCode.REQUEST_VALIDATION_ERROR) from exc


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
    """这次调用的元数据：身份、计划名、耗时、模型调用次数、**一个**停止原因与三条不丢信息的字段。

    ``stop_reason`` 就是 ``app.services.research_limits.StopReason`` 的那七个词之一：
    响应、日志、统计读同一套词（父 spec #138 C 段）。类型直接取那个枚举而不是 ``str``，
    于是 OpenAPI 里也是一份穷举清单——**加第八个原因要先改那一处枚举**，改不动就说明
    它不该在这里出现。把成本写在这里是 ADR-0026 §5 的要求——「每次 Research 花多久、
    几次模型调用」必须可读，否则它会被当成普通检索来用。

    三条**不丢信息**的字段（票 #149），各自回答一个别的字段回答不了的问题：

    * ``trace_id``：这次调用的身份（32 位十六进制，与归属索引那一行**同一个值**）。一次
      Research 只有一行归属索引，所以这根线能直接翻回那一行——子检索不各写一行；
    * ``evidence_state``：``complete`` / ``partial`` / ``missing``，由核心片数算出来。
      ``stop_reason`` 说的是「为什么停」，**说不出一件事**：材料够不够。两者都要有；
    * ``sub_search_degraded_reasons``：子检索自身的降级原因（一因一码的固定串，去重）。
      没有它，一次「关键词一路挂了、只是碰巧向量一路也命中了」的 Research 看起来完全正常；
    * ``strong_model_fallback``：生成阶段是不是快模型兜的底，以及为什么（``strong model
      unavailable`` / ``strong model failed``）。正常路径是 ``null``——「没有降级」不能被
      一个占位串说成有。
    """

    plan_name: str
    duration_ms: int
    model_calls: int
    stop_reason: StopReason
    trace_id: str
    #: 三态完整度。词表与 ``app.services.orchestration`` 的 ``COMPLETENESS_*`` 是同一份，
    #: 做成 ``Literal`` 是为了让 OpenAPI 里也是一份穷举清单（不是让第三个词冒出来）。
    evidence_state: Literal["complete", "partial", "missing"] = "missing"
    sub_search_degraded_reasons: list[str] = Field(default_factory=list)
    strong_model_fallback: str | None = None


class ResearchCitation(BaseModel):
    """一条引用块：文本 + 证据下标 + 状态（形状取自 ADR-0026 §2）。

    ``evidence_ids`` 是 ``E1`` 起编号的证据下标，按**最终材料顺序**分配（``E1`` =
    ``materials[0]``）：同一次调用内不复用、不重排，所以调用方可以在自己的答复里直接引用
    「E3」。``status = insufficient`` 的块下标恒为空——「材料不足」既不许编内容，也不许
    挂一条证据（四条硬校验的第二条）。
    """

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["supported", "insufficient"]


class ResearchData(BaseModel):
    """一次 Research 的响应数据。

    ``materials`` 复用检索的 :class:`~app.schemas.rag.RetrievedChunk`：材料就是交付单元
    （一节一条），同一个值对象让「材料的形状」不可能与 ``/rag/retrieve`` 漂移。

    ``answer`` 是引用块的**渲染串**，``citations`` 是那几块：前者不承载任何后者以外的
    内容（ADR-0026 §2）。四条硬校验任一不过 → ``answer = null``、``citations = []``，
    而材料、计划、轮次、停止原因照给——调用方自己组织答复仍然拿得到全部素材。
    """

    query: str
    kb_ids: list[str]
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
    trace_id: str,
) -> dict[str, Any]:
    """把状态机的产物装成响应形状（唯一一处装填，HTTP 与将来的 MCP 共用）。

    ``outcome`` 是 :class:`app.services.research.ResearchOutcome`：这里只做「换外壳」，
    不重算任何判定——材料、计划、计数、停止原因、完整度全部原样搬过来。判定散在两处，
    就会出现「响应说两轮、日志说一轮」这种无法自证的错误。

    ``trace_id`` 由**外层**递进来（``app.services.research_gate`` 生成 ``log_id`` 之后派生）：
    身份先于旁路，所以它不依赖那一次写库是否成功。
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
            "trace_id": trace_id,
            "evidence_state": outcome.evidence_state,
            "sub_search_degraded_reasons": list(outcome.sub_search_degraded),
            "strong_model_fallback": outcome.strong_model_fallback,
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
    "parse_research_request",
    "payload_of",
]
