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

    api_key = create_api_key_row(
        db, tenant_id=tenant_id, policy_id=policy_id, label=label
    )
    # 提交点不在这里：脚本用 ``with transaction(db)`` 圈住，请求路径由路由的事务依赖兜住。
    return api_key


def create_api_key_row(
    db: Session, *, tenant_id: str, policy_id: str, label: str | None
) -> ApiKey:
    """只建 Key 行、只 flush：控制面签发要和配置与审计放进同一个事务。

    Key 原文的生成收在 :func:`app.utils.id_generator.new_api_key_value`；这里只负责拼行。
    """

    api_key = ApiKey(
        id=new_id(),
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_value=new_api_key_value(),
        label=label,
        status=API_KEY_STATUS_ACTIVE,
    )
    api_key_repository.create(db, api_key)
    return api_key
