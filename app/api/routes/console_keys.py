"""控制面 Key 与密钥配置路由（票 #91），自带 ``prefix="/console"``。

**独立成一个模块而不是往 ``app/api/routes/console.py`` 里追加**：同一个目录下有四五张并行
票各加几条控制面接口，共用一个文件必然天天冲突；每条票在自己的模块里自带前缀、由
``app/main.py`` 用 ``include_router`` 平级挂上，像 ``console`` 那样。

这一层只做「取参数、调服务、拼响应」：跨租户收口、自洽校验与审计全在
:mod:`app.services.key_admin_service`。租户门禁与写头统一走 ``app/api/console_deps.py`` 的依赖
（读 ``require_console_tenant``、写 ``require_console_tenant_write``；它们内部判
``x-requested-with``，缺了就是 ``20011``）——**不要在这层再写一遍头校验或租户判定**，
服务层只接收依赖已经校验过的身份（#82 修复：判定以前在本文件与服务层各写了一遍）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import (
    require_console_tenant,
    require_console_tenant_write,
)
from app.core.console_auth import ConsoleIdentity
from app.db.async_session import get_async_db, get_async_transaction
from app.db.base import now as now_of
from app.db.transaction import AsyncTransaction
from app.schemas.common import ApiResponse, ok
from app.schemas.console_keys import (
    IssuedKeyData,
    KeyDeleteData,
    KeyIssueRequest,
    KeyListData,
    KeyPolicyPatchData,
    KeyPolicyPatchRequest,
    RevokePreviousData,
    issued_key_data_of,
    key_data_of,
)
from app.services import key_admin_service

router = APIRouter(prefix="/console", tags=["console"])


@router.get(
    "/keys",
    response_model=ApiResponse[KeyListData],
    summary="本租户的 Key 列表（打码形态 + 它引用的配置）",
)
async def list_keys(
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[KeyListData]:
    """列出本租户的 Key。**只有打码形态**（``rc_live_…4f3a2``）：原文在签发之后就不存在了。

    这条路径（含身份判定）在异步栈上（票 #129）。签发 / 删除 / 改配置也一样——它们在
    ADR-0024 之后同属异步栈，写路径另外声明 ``get_async_transaction``（票 #130）。
    """

    rows = await key_admin_service.list_keys(db, actor=identity)
    return ok(KeyListData(keys=[key_data_of(row.api_key, row.policy) for row in rows]))


@router.post(
    "/keys",
    response_model=ApiResponse[IssuedKeyData],
    summary="签发 Key（连带建一条密钥配置，完整 Key 只在本响应里出现一次）",
)
async def issue_key(
    payload: KeyIssueRequest,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[IssuedKeyData]:
    """签发：``{label, policy}`` → 一把新 Key + 它引用的配置 + 一条审计。

    ``key_plaintext`` 是**唯一**一次能看到完整 Key 的地方：库里只有 scrypt 哈希，此后
    连平台管理员都取不回来。前端必须在这一刻把它交给人（复制 / 直接去调试台试一把）。

    配置不自洽（档位要的能力没开、超套餐上限）→ ``20015``，两行都不落库。
    """

    issued = await key_admin_service.issue_key(
        db,
        actor=identity,
        label=payload.label,
        policy_values=payload.policy.provided(),
    )
    return ok(issued_key_data_of(issued.api_key, issued.policy, issued.plaintext))


@router.post(
    "/keys/{key_id}/regenerate",
    response_model=ApiResponse[IssuedKeyData],
    summary="重新生成这把 Key 的密钥本体（旧 Key 进入 24 小时过渡期）",
)
async def regenerate_key(
    key_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[IssuedKeyData]:
    """轮换：**同一把 Key**（``key_id`` / 名字 / 白名单 / 额度都不变）换一份密钥本体。

    旧密钥本体进入 **24 小时过渡期**（窗口内新旧都能过校验），过渡期只保留最近一把：
    窗口内再调一次本接口，上一把过渡密钥直接作废。

    ``key_plaintext`` 同样是**唯一**一次能看到完整 Key 的地方。响应里的
    ``prev_key_expires_at`` 是这次写进那一列的过渡截止时刻，前端据此说清「旧 Key 还能用到几点」。
    """

    regenerated = await key_admin_service.regenerate_key(
        db, actor=identity, key_id=key_id, now=now_of()
    )
    return ok(
        issued_key_data_of(regenerated.api_key, regenerated.policy, regenerated.plaintext)
    )


@router.post(
    "/keys/{key_id}/revoke-previous",
    response_model=ApiResponse[RevokePreviousData],
    summary="把过渡密钥提前作废（不必等 24 小时窗口到期）",
)
async def revoke_previous_secret(
    key_id: str = Path(..., min_length=1),
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[RevokePreviousData]:
    """立刻让过渡密钥失效。**幂等**：没有过渡密钥时回 ``revoked=false``，不报错。"""

    revoked = await key_admin_service.revoke_previous_secret(
        db, actor=identity, key_id=key_id, now=now_of()
    )
    return ok(RevokePreviousData(key_id=key_id, revoked=revoked))


@router.delete(
    "/keys/{key_id}",
    response_model=ApiResponse[KeyDeleteData],
    summary="删 Key（不删它的配置）",
)
async def delete_key(
    key_id: str,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[KeyDeleteData]:
    """删 Key。配置独立于 Key，删凭证不连带删配置；跨租户按不存在（``20016``）。"""

    deleted = await key_admin_service.delete_key(db, actor=identity, key_id=key_id)
    return ok(KeyDeleteData(key_id=deleted.key_id, deleted=True))


@router.patch(
    "/key-policies/{policy_id}",
    response_model=ApiResponse[KeyPolicyPatchData],
    summary="改一套密钥配置（部分更新，改完下一次调用即生效）",
)
async def patch_key_policy(
    policy_id: str,
    payload: KeyPolicyPatchRequest,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[KeyPolicyPatchData]:
    """改配置的五组字段；档位越出套餐白名单 / 超限额 → ``20015``，跨租户 → ``20016``。"""

    result = await key_admin_service.patch_key_policy(
        db,
        actor=identity,
        policy_id=policy_id,
        changes=payload.provided(),
    )
    return ok(
        KeyPolicyPatchData(
            policy_id=result.policy_id,
            before=result.before,
            after=result.after,
            changed=list(result.changed),
        )
    )
