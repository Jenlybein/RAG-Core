"""建知识库 / 列知识库接口。

API 层只做入参出参，业务在 KnowledgeBaseService / Repository。
头部带 API Key，tenant_id 由服务端从 Key 解出，不进请求体；列库再按这把 Key 的数据范围
收窄（越界的库不出现，见 ``app.services.data_scope``）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_key
from app.core.auth import DevKeyContext
from app.db.session import get_db, get_transaction
from app.db.transaction import Transaction
from app.models.knowledge_base import formats_of, settings_of
from app.repositories import knowledge_base_repository
from app.schemas.common import ApiResponse, ok
from app.schemas.knowledge_base import (
    CreateKBData,
    CreateKBRequest,
    KnowledgeBaseItem,
    KnowledgeBaseListData,
)
from app.services import knowledge_base_service
from app.services.data_scope import scoped_kb_ids

router = APIRouter()


@router.post(
    "/knowledge-bases/create",
    response_model=ApiResponse[CreateKBData],
    summary="创建知识库",
)
async def create_knowledge_base(
    payload: CreateKBRequest,
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(get_current_key),
    tx: Transaction = Depends(get_transaction),
) -> ApiResponse[CreateKBData]:
    data = await knowledge_base_service.create_kb(
        db,
        name=payload.name,
        description=payload.description,
        tenant_id=key.tenant_id,
        split_strategy=payload.split_strategy,
        plan=key.plan,
        chunk_size=payload.chunk_size,
        allowed_formats=payload.allowed_formats,
    )
    return ok(CreateKBData.model_validate(data))


@router.get(
    "/knowledge-bases",
    response_model=ApiResponse[KnowledgeBaseListData],
    summary="列这把 Key 可见的知识库",
)
async def list_knowledge_bases(
    db: Session = Depends(get_db),
    key: DevKeyContext = Depends(get_current_key),
) -> ApiResponse[KnowledgeBaseListData]:
    rows = knowledge_base_repository.list_by_tenant(db, key.tenant_id)
    # 第二道关卡的另一半：这一层是「租户里有哪些库」，还要再按这把 Key 的数据范围收窄。
    # 列库与检索判的是同一份范围，只是反应不同——检索撞到范围外必须拒绝（那是「这次
    # 要查什么」的一部分），清单则把不可见的库直接去掉，不为一条不可见的库让整个清单失败。
    visible = set(scoped_kb_ids(policy=key.policy, kb_ids=[row.id for row in rows]))
    rows = [row for row in rows if row.id in visible]
    data = KnowledgeBaseListData(
        knowledge_bases=[
            KnowledgeBaseItem(
                kb_id=row.id,
                name=row.name,
                tenant_id=row.tenant_id,
                split_strategy=row.split_strategy,
                allowed_formats=formats_of(row),
                chunk_size=settings_of(row)["chunk_size"],
            )
            for row in rows
        ]
    )
    return ok(data)
