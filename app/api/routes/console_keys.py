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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.console_deps import require_console_tenant, require_console_tenant_write
from app.core.console_auth import ConsoleIdentity
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.console_keys import (
    KeyData,
    KeyDeleteData,
    KeyIssueRequest,
    KeyListData,
    KeyPolicyPatchData,
    KeyPolicyPatchRequest,
    key_data_of,
)
from app.services import key_admin_service

router = APIRouter(prefix="/console", tags=["console"])


@router.get(
    "/keys",
    response_model=ApiResponse[KeyListData],
    summary="本租户的 Key 列表（明文 + masked + 它引用的配置）",
)
def list_keys(
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: Session = Depends(get_db),
) -> ApiResponse[KeyListData]:
    """列出本租户的 Key。明文是本票的明确决定（ADR-0004），masked 是第二形态。"""

    rows = key_admin_service.list_keys(db, actor=identity)
    return ok(KeyListData(keys=[key_data_of(row.api_key, row.policy) for row in rows]))


@router.post(
    "/keys",
    response_model=ApiResponse[KeyData],
    summary="签发 Key（连带建一条密钥配置）",
)
def issue_key(
    payload: KeyIssueRequest,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: Session = Depends(get_db),
) -> ApiResponse[KeyData]:
    """签发：``{label, policy}`` → 一把新 Key + 它引用的配置 + 一条审计。

    配置不自洽（档位要的能力没开、超套餐上限）→ ``20015``，两行都不落库。
    """

    issued = key_admin_service.issue_key(
        db,
        actor=identity,
        label=payload.label,
        policy_values=payload.policy.provided(),
    )
    return ok(key_data_of(issued.api_key, issued.policy))


@router.delete(
    "/keys/{key_id}",
    response_model=ApiResponse[KeyDeleteData],
    summary="删 Key（不删它的配置）",
)
def delete_key(
    key_id: str,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: Session = Depends(get_db),
) -> ApiResponse[KeyDeleteData]:
    """删 Key。配置独立于 Key，删凭证不连带删配置；跨租户按不存在（``20016``）。"""

    deleted = key_admin_service.delete_key(db, actor=identity, key_id=key_id)
    return ok(KeyDeleteData(key_id=deleted.key_id, deleted=True))


@router.patch(
    "/key-policies/{policy_id}",
    response_model=ApiResponse[KeyPolicyPatchData],
    summary="改一套密钥配置（部分更新，改完下一次调用即生效）",
)
def patch_key_policy(
    policy_id: str,
    payload: KeyPolicyPatchRequest,
    identity: ConsoleIdentity = Depends(require_console_tenant_write),
    db: Session = Depends(get_db),
) -> ApiResponse[KeyPolicyPatchData]:
    """改配置的五组字段；档位越出套餐白名单 / 超限额 → ``20015``，跨租户 → ``20016``。"""

    result = key_admin_service.patch_key_policy(
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

