"""检索接口：POST /api/v1/rag/retrieve。

只返回结构化切片 + 分数 + 元数据；不返回 context_text，不生成答案。
头部带 API Key，tenant_id 由服务端从 Key 解出，不进请求体；哪些库能查由这把 Key 的
密钥配置圈定（六道关卡的第二道，见 ``app.services.data_scope``）。

请求体只有 ``kb_ids`` + ``query``（ADR-0010「第三次修订」）。检索方式、返回条数、
两个结果形状开关**从密钥配置解析**（``app.services.retrieval_options``）——请求里
既没有这些字段，也没有任何一处代码去读它们。
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
from app.services.retrieval_options import options_from_policy

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

    # 这次调用「用多好」只由密钥配置决定：请求体里连字段都没有，所以质量参数在这条
    # 链路上只有一个来源，不会出现「请求覆盖配置」的第二条路径。
    data = await rag_service.retrieve(
        db,
        tenant_id=key.tenant_id,
        kb_ids=payload.kb_ids,
        query=payload.query,
        options=options_from_policy(policy=key.policy),
    )
    return ok(RetrieveData.model_validate(data))
