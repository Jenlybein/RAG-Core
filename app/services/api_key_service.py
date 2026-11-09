"""签发 API Key：绑定一套密钥配置，生成 ``rc_live_`` 原文。

三条前置检查都在写之前做完，失败时一行都不落库：

1. 租户必须存在——Key 没有租户就没有配额主体；
2. 配置必须存在——指向不存在的配置的 Key 是最难查的一类事故（它永远鉴权失败，
   而失败信息只会说「Key 无效」）；
3. 配置必须属于同一个租户——跨租户引用等于把 A 的额度与数据范围发给 B。

本期 Key 明文入库（ADR-0004）：脚本与控制台都能看到原文，这是给调试台留的能力。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.api_key import ApiKey
from app.repositories import api_key_repository, key_policy_repository, tenant_repository
from app.utils.id_generator import new_api_key_value, new_id


def issue_api_key(
    db: Session,
    *,
    tenant_id: str,
    policy_id: str,
    label: str | None = None,
) -> ApiKey:
    """给租户签一把绑定到指定密钥配置的 Key，返回刚写入的行（含原文）。"""

    if tenant_repository.get_by_id(db, tenant_id) is None:
        raise LookupError(f"租户不存在：{tenant_id}")

    policy = key_policy_repository.get_by_id(db, policy_id)
    if policy is None:
        raise LookupError(f"密钥配置不存在：{policy_id}")
    if policy.tenant_id != tenant_id:
        raise ValueError(
            f"密钥配置 {policy_id} 不属于租户 {tenant_id}：跨租户引用一律拒绝"
        )

    api_key = ApiKey(
        id=new_id(),
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_value=new_api_key_value(),
        label=label,
    )
    api_key_repository.create(db, api_key)
    db.commit()
    return api_key
