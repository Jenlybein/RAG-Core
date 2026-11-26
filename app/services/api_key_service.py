"""签发 API Key：绑定一套密钥配置，生成 ``rc_live_<id>.<32 位密钥>``。

三条前置检查都在写之前做完，失败时一行都不落库：

1. 租户必须存在——Key 没有租户就没有配额主体；
2. 配置必须存在——指向不存在的配置的 Key 是最难查的一类事故（它永远鉴权失败，
   而失败信息只会说「Key 无效」）；
3. 配置必须属于同一个租户——跨租户引用等于把 A 的额度与数据范围发给 B。

**库里只存哈希**：``key_hash`` 是密钥本体的 scrypt 哈希，``key_tail`` 是末 5 位（只给面板
展示）。原文只在签发这一刻存在，所以这一层的返回值是 :class:`MintedKey`——把「要落库的
那一行」与「只存在这一刻的原文」并排交出去，而不是把原文悄悄挂在一个字段上等下一个人
来猜。调用方（运维脚本、控制面路由）各自决定怎么把它交回给人。

**行的拼装只有一份，落库有两层薄壳**（第 11 步票 #130）：:func:`_mint` 是纯函数
（生成原文、算哈希、拼行），同步 :func:`create_api_key_row` 给运维脚本
（``scripts/issue_api_key.py``），异步 :func:`create_api_key_row_async` 给控制面签发。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.passwords import hash_password
from app.models.api_key import ApiKey
from app.repositories import api_key_repository, key_policy_repository, tenant_repository
from app.utils.id_generator import api_key_tail_of, new_api_key_value, new_id, split_api_key_value


#: 新 Key 的状态。显式写出来、不依赖列默认值：列默认值哪天被改掉，签发出来的 Key 会
#: 带着一个没人预期的状态，而签发路径不会有任何异常。
API_KEY_STATUS_ACTIVE = "active"


@dataclass(frozen=True)
class MintedKey:
    """一次生成的产物：**要落库的那一行** + **只在签发这一刻存在的原文**。

    两个字段刻意并排：``api_key`` 里没有原文可读（只有哈希与末 5 位），想把它交回给人的
    调用方必须显式用 ``plaintext``，于是「原文从哪来」在类型上就只有一条路。
    """

    api_key: ApiKey
    plaintext: str


def issue_api_key(
    db: Session,
    *,
    tenant_id: str,
    policy_id: str,
    label: str | None = None,
) -> MintedKey:
    """给租户签一把绑定到指定密钥配置的 Key，返回刚写入的行与原文。

    这是**脚本的同步路径**（``scripts/issue_api_key.py``）；控制面走
    :func:`app.services.key_admin_service.issue_key`（异步会话 + 同事务审计）。
    """

    if tenant_repository.get_by_id(db, tenant_id) is None:
        raise LookupError(f"租户不存在：{tenant_id}")

    policy = key_policy_repository.get_by_id(db, policy_id)
    if policy is None:
        raise LookupError(f"密钥配置不存在：{policy_id}")
    if policy.tenant_id != tenant_id:
        raise ValueError(f"密钥配置 {policy_id} 不属于租户 {tenant_id}：跨租户引用一律拒绝")

    minted = create_api_key_row(db, tenant_id=tenant_id, policy_id=policy_id, label=label)
    # 提交点不在这里：脚本用 ``with transaction(db)`` 圈住，请求路径由路由的事务依赖兜住。
    return minted


def _mint(*, tenant_id: str, policy_id: str, label: str | None) -> MintedKey:
    """拼一行新 Key（**纯函数**：不碰会话）。

    生成 → 拆 → 取密钥本体：格式只有 :func:`app.utils.id_generator.new_api_key_value`
    一处定义，这里把它拆回去而不是另算一份密钥，于是「串里那个 secret」与「被哈希的那个
    secret」在构造上不可能不是同一个。
    """

    key_id = new_id()
    plaintext = new_api_key_value(key_id)
    parsed = split_api_key_value(plaintext)
    assert parsed is not None, "自己刚生成的 Key 必须能被自己的格式拆开"
    _parsed_key_id, secret = parsed

    api_key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_hash=hash_password(secret),
        key_tail=api_key_tail_of(secret),
        label=label,
        status=API_KEY_STATUS_ACTIVE,
    )
    return MintedKey(api_key=api_key, plaintext=plaintext)


def create_api_key_row(
    db: Session, *, tenant_id: str, policy_id: str, label: str | None
) -> MintedKey:
    """只建 Key 行、只 flush（同步外壳：运维脚本用）。"""

    minted = _mint(tenant_id=tenant_id, policy_id=policy_id, label=label)
    api_key_repository.create(db, minted.api_key)
    return minted


async def create_api_key_row_async(
    db: AsyncSession, *, tenant_id: str, policy_id: str, label: str | None
) -> MintedKey:
    """只建 Key 行、只 flush（控制面签发要和配置与审计放进同一个事务）。"""

    minted = _mint(tenant_id=tenant_id, policy_id=policy_id, label=label)
    await api_key_repository.create_async(db, minted.api_key)
    return minted
