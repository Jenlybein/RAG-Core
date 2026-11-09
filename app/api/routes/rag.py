"""检索接口：POST /api/v1/rag/retrieve。

只返回结构化切片 + 分数 + 元数据；不返回 context_text，不生成答案。
头部带 API Key，tenant_id 由服务端从 Key 解出，不进请求体；哪些库能查由这把 Key 的
密钥配置圈定（六道关卡的第二道，见 ``app.services.data_scope``）。
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

    data = await rag_service.retrieve(
        db,
        tenant_id=key.tenant_id,
        kb_ids=payload.kb_ids,
        query=payload.query,
        mode=payload.mode,
        top_k=payload.top_k,
        include_parent=payload.include_parent,
        expand_table_rows=payload.expand_table_rows,
    )
    return ok(RetrieveData.model_validate(data))
