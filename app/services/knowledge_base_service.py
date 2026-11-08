"""建库业务：生成 kb_id → 校验配置 → 落库 → 提交。

Service 是全链路唯一 commit 的地方；Repository 与 Provider 都不提交。

策略与白名单在这里一次性定下来：``resolve_formats`` 既补默认值也拒绝矛盾组合。
把校验放服务层而不是路由：将来若有第二个建库入口（CLI、导入），它同样受约束。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.models.knowledge_base import (
    DEFAULT_CHUNK_SIZE,
    KnowledgeBase,
    resolve_formats,
    settings_of,
)
from app.repositories import knowledge_base_repository
from app.utils.id_generator import new_kb_id


async def create_kb(
    db: Session,
    *,
    name: str,
    description: str | None,
    tenant_id: str,
    split_strategy: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    allowed_formats: Sequence[str] | None = None,
) -> dict:
    try:
        formats = resolve_formats(split_strategy, allowed_formats)
    except ValueError as exc:
        # 矛盾组合（structure 配 pdf）与拼错的格式名都是调用方的参数问题，不是系统故障。
        raise AppError(ErrorCode.PARAM_ERROR, str(exc)) from exc

    kb = KnowledgeBase(
        id=new_kb_id(),
        tenant_id=tenant_id,
        name=name,
        description=description,
        split_strategy=split_strategy,
        allowed_formats=formats,
        settings={"chunk_size": chunk_size},
    )
    knowledge_base_repository.create(db, kb)
    db.commit()
    values = settings_of(kb)
    return {
        "kb_id": kb.id,
        "name": kb.name,
        "tenant_id": kb.tenant_id,
        "split_strategy": kb.split_strategy,
        "allowed_formats": formats,
        "chunk_size": values["chunk_size"],
        "created_at": kb.created_at,
    }
