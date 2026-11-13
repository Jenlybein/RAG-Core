"""签发 API Key：绑定一套密钥配置，生成 ``rc_live_`` 原文。

三条前置检查都在写之前做完，失败时一行都不落库：

1. 租户必须存在——Key 没有租户就没有配额主体；
2. 配置必须存在——指向不存在的配置的 Key 是最难查的一类事故（它永远鉴权失败，
   而失败信息只会说「Key 无效」）；
3. 配置必须属于同一个租户——跨租户引用等于把 A 的额度与数据范围发给 B。

本期 Key 明文入库（ADR-0004）：脚本与控制台都能看到原文，这是给调试台留的能力。

**行的拼装只有一份，落库有两层薄壳**（第 11 步票 #130）：:func:`_new_key_row` 是纯函数
（生成原文、拼行），同步 :func:`create_api_key_row` 给运维脚本
（``scripts/issue_api_key.py``），异步 :func:`create_api_key_row_async` 给控制面签发。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey
from app.repositories import api_key_repository, key_policy_repository, tenant_repository
from app.utils.id_generator import new_api_key_value, new_id


#: 新 Key 的状态。显式写出来、不依赖列默认值：列默认值哪天被改掉，签发出来的 Key 会
#: 带着一个没人预期的状态，而签发路径不会有任何异常。
API_KEY_STATUS_ACTIVE = "active"


def issue_api_key(
    db: Session,
    *,
    tenant_id: str,
    policy_id: str,
    label: str | None = None,
) -> ApiKey:
    """给租户签一把绑定到指定密钥配置的 Key，返回刚写入的行（含原文）。

    这是**脚本的同步路径**（``scripts/issue_api_key.py``）；控制面走
    :func:`app.services.key_admin_service.issue_key`（异步会话 + 同事务审计）。
    """

    if tenant_repository.get_by_id(db, tenant_id) is None:
        raise LookupError(f"租户不存在：{tenant_id}")

    policy = key_policy_repository.get_by_id(db, policy_id)
    if policy is None:
        raise LookupError(f"密钥配置不存在：{policy_id}")
    if policy.tenant_id != tenant_id:
        raise ValueError(
            f"密钥配置 {policy_id} 不属于租户 {tenant_id}：跨租户引用一律拒绝"
        )

    api_key = create_api_key_row(
        db, tenant_id=tenant_id, policy_id=policy_id, label=label
    )
    # 提交点不在这里：脚本用 ``with transaction(db)`` 圈住，请求路径由路由的事务依赖兜住。
    return api_key


def _new_key_row(*, tenant_id: str, policy_id: str, label: str | None) -> ApiKey:
    """拼一行新 Key（**纯函数**：不碰会话，原文由 :func:`new_api_key_value` 生成）。"""

    return ApiKey(
        id=new_id(),
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_value=new_api_key_value(),
        label=label,
        status=API_KEY_STATUS_ACTIVE,
    )


def create_api_key_row(
    db: Session, *, tenant_id: str, policy_id: str, label: str | None
) -> ApiKey:
    """只建 Key 行、只 flush（同步外壳：运维脚本用）。"""

    api_key = _new_key_row(tenant_id=tenant_id, policy_id=policy_id, label=label)
    api_key_repository.create(db, api_key)
    return api_key


async def create_api_key_row_async(
    db: AsyncSession, *, tenant_id: str, policy_id: str, label: str | None
) -> ApiKey:
    """只建 Key 行、只 flush（控制面签发要和配置与审计放进同一个事务）。"""

    api_key = _new_key_row(tenant_id=tenant_id, policy_id=policy_id, label=label)
    await api_key_repository.create_async(db, api_key)
    return api_key
