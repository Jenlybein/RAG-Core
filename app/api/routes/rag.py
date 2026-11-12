"""检索与反馈接口：``POST /api/v1/rag/retrieve``、``POST /api/v1/rag/feedback``。

检索只返回结构化切片 + 分数 + 元数据；不返回 context_text，不生成答案。
头部带 API Key，tenant_id 由服务端从 Key 解出，不进请求体；哪些库能查由这把 Key 的
密钥配置圈定（六道关卡的第二道，见 ``app.services.data_scope``）。

请求体只有 ``kb_ids`` + ``query``（ADR-0010「第三次修订」）。检索方式、返回条数、
重排、改写与两个结果形状开关**从密钥配置解析**，而且要过五道门禁
（``app.services.profile_policy``）——请求里既没有这些字段，也没有任何一处代码去读它们。

本路由只做两件判定之外的事：把身份与请求递进 ``app.services.retrieval_gate``（白名单 →
档位门禁 → 配额 → 召回 → 记账的唯一实现），再把已经校验成形的数据装进 ``{code, msg,
data}`` 外壳。判定本身不在这里：判定写进路由，MCP 一侧就得再写一遍。

**两个会话依赖，各管各的**（第 11 步票 #127）：``get_async_db`` 给检索链路（异步栈），
``get_db`` 给身份判定（``get_current_key`` 是同步依赖，判定内核与控制面还没搬）。
把检索挪到异步栈之后，这条路由上**没有任何一次同步读库**；身份那一条仍是同步的，
它属于 #129 / #130 的范围。

反馈（#105）走同一个身份依赖，但**没有关卡**：它不改检索链路，只对「已经发生过的那一次」
打分。判定（分数区间、归属、``trace_id`` 对得上）全在
``app.services.retrieval_feedback`` 里，路由只转发。

**反馈这一条已经换到异步会话**（第 11 步票 #128）：它声明 ``get_async_db``，于是归属校验
那一读与本地写入那一笔都在 ``AsyncSession`` 上。检索（``/rag/retrieve``）仍是同步会话，
随票 #127 搬——同一份文件里两条路由各用一套会话，是双栈期的显式事实，不是设计。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_key
from app.core.auth import DevKeyContext
from app.db.async_session import get_async_db
from app.schemas.common import ApiResponse, ok
from app.schemas.feedback import FeedbackData, FeedbackRequest
from app.schemas.rag import RetrieveData, RetrieveRequest
from app.services import retrieval_feedback
from app.services.retrieval_gate import gated_retrieve

router = APIRouter()


@router.post("/rag/retrieve", response_model=ApiResponse[RetrieveData], summary="检索相关切片")
async def retrieve(
    payload: RetrieveRequest,
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[RetrieveData]:
    # 六道关卡的顺序与判定只在 gated_retrieve 里，MCP 一侧调的是同一个函数：越权、超限
    # 与配额用尽都在那里失败，本路由不判规则、也不各长一套。
    result = await gated_retrieve(
        db,
        key=key,
        kb_ids=payload.kb_ids,
        query=payload.query,
    )
    return ok(result)


@router.post("/rag/feedback", response_model=ApiResponse[FeedbackData], summary="给一次检索打分")
async def feedback(
    payload: FeedbackRequest,
    db: AsyncSession = Depends(get_async_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[FeedbackData]:
    """对某一次检索打 1~5 分；**本地说了算**，观测服务挂掉照样成功。

    成功响应回显**最终**分数：同一个 ``log_id`` 再打一次是覆盖（后写覆盖），
    所以调用方重试不必担心留下两条互相矛盾的行。
    """

    data = await retrieval_feedback.submit_feedback(
        db=db,
        tenant_id=key.tenant_id,
        log_id=payload.log_id,
        trace_id=payload.trace_id,
        score=payload.score,
        comment=payload.comment,
    )
    return ok(FeedbackData.model_validate(data))
