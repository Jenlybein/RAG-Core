"""建库业务：生成 kb_id → 校验配置 → 落库。

**这里不提交**：提交点是调用方的边界（数据面路由的 ``Depends(get_async_transaction)``，
或控制面那条把「建库 + 审计」圈进同一笔的路由依赖）。仓储只 ``flush``，服务只写，
全仓唯一的提交点在 :mod:`app.db.transaction`。

策略与白名单在这里一次性定下来：``resolve_formats`` 既补默认值也拒绝矛盾组合。
把校验放服务层而不是路由：将来若有第二个建库入口（CLI、导入），它同样受约束。

静态配额（库数）也在这里，理由同上：它是「这个租户买得起几个库」，必须落在写路径上，
而不是只挂在控制台接口上——否则脚本与将来的第二个入口就成了绕过套餐的后门。

**会话是 ``AsyncSession``**（第 11 步票 #130）：两个入口（数据面、控制面）都换到了异步
栈上，这一份实现只有一种会话可用。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.models.knowledge_base import (
    DEFAULT_CHUNK_SIZE,
    KnowledgeBase,
    resolve_formats,
    settings_of,
)
from app.models.plan import PlanSpec
from app.repositories import knowledge_base_repository
from app.services import static_quota
from app.utils.id_generator import new_kb_id


async def create_kb(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    tenant_id: str,
    split_strategy: str,
    plan: PlanSpec,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    allowed_formats: Sequence[str] | None = None,
) -> dict:
    """建库：判定（策略 / 配额 / 格式）→ 落一行，**不提交**。

    两个入口（数据面、控制面）共用这一份判定；控制面额外写一条审计，两条写入由同一个
    请求事务一起提交，于是「建了库、却没账本」的窗口不存在。
    """

    try:
        formats = resolve_formats(split_strategy, allowed_formats)
    except ValueError as exc:
        # 矛盾组合（structure 配 pdf）与拼错的格式名都是调用方的参数问题，不是系统故障。
        raise AppError(ErrorCode.PARAM_ERROR, str(exc)) from exc

    # 套餐校验在构造行之前：被拒的请求不生成 id、不落行（建第 N+1 个库是 20014）。
    await static_quota.ensure_kb_quota(db, tenant_id=tenant_id, plan=plan)

    kb = KnowledgeBase(
        id=new_kb_id(),
        tenant_id=tenant_id,
        name=name,
        description=description,
        split_strategy=split_strategy,
        allowed_formats=formats,
        settings={"chunk_size": chunk_size},
        # 建库即声明生效索引版本（ADR-0025）：本步的索引流水线仍把切片写成
        # ``DEFAULT_INDEX_VERSION``，没有别的版本可查，所以新库的生效版本与产物同值。
        # 不写它的话，这个库的切片版本与库的「生效版本」对不上，检索一条都匹配不到——
        # 那正是 ADR-0025 决策七说的「不报错、只是搜不到」。真正的版本轮换（建 v2、
        # 校验四项计数、单事务切换）是 #142 的激活流程，它会接管这一列。
        active_index_version=DEFAULT_INDEX_VERSION,
    )
    await knowledge_base_repository.create(db, kb)
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
