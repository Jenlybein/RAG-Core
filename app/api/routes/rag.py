"""检索接口：POST /api/v1/rag/retrieve。

只返回结构化切片 + 分数 + 元数据；不返回 context_text，不生成答案。
头部带 API Key，tenant_id 由服务端从 Key 解出，不进请求体；哪些库能查由这把 Key 的
密钥配置圈定（六道关卡的第二道，见 ``app.services.data_scope``）。

请求体只有 ``kb_ids`` + ``query``（ADR-0010「第三次修订」）。检索方式、返回条数、
重排、改写与两个结果形状开关**从密钥配置解析**，而且要过五道门禁
（``app.services.tier_policy``）——请求里既没有这些字段，也没有任何一处代码去读它们。

本路由只做两件判定之外的事：按顺序**调用**那两道关卡（白名单 → 套餐 / 档位 / 库数），
以及把解析好的参数交给组合根。判定本身不在这里：判定写进路由，MCP 一侧就得再写一遍。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_key
from app.core.auth import DevKeyContext
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.rag import RetrieveData, RetrieveRequest
from app.services import rag_service
from app.services.data_scope import ensure_kb_scope
from app.services.tier_policy import resolve_retrieval_options

router = APIRouter()


@router.post("/rag/retrieve", response_model=ApiResponse[RetrieveData], summary="检索相关切片")
async def retrieve(
    payload: RetrieveRequest,
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[RetrieveData]:
    # 第二道关卡：白名单。身份已经解出来（``key`` 就在手上），限流、配额与召回都还没
    # 开始——顺序反了会让越权请求先扣掉客户当天的额度（ADR-0018「六道关卡」）。
    ensure_kb_scope(policy=key.policy, kb_ids=payload.kb_ids)

    # 第三 / 第四道关卡：套餐复核 + 档位展开与门禁 + 单次库数。同一份实现，MCP 一侧
    # 调的也是它；越权在这里就失败，一次嵌入都不该烧。
    options = resolve_retrieval_options(
        policy=key.policy,
        plan=key.plan,
        kb_count=len(payload.kb_ids),
    )

    data = await rag_service.retrieve(
        db,
        tenant_id=key.tenant_id,
        kb_ids=payload.kb_ids,
        query=payload.query,
        options=options,
    )
    return ok(RetrieveData.model_validate(data))
