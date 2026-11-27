"""控制面 Key 与密钥配置的业务层（票 #91）：列 / 签发 / 删 / 改配置。

这一层只做三件路由不该做的事（租户门禁不在其中，它归 HTTP 层，见第 1 条）：

1. **只接收已校验的身份**。``/console/keys`` 与 ``/console/key-policies`` 都是用户面板接口，
   对 ``tenant_id IS NULL`` 的平台管理员一律 ``20011``，但这条判定只在 HTTP 层的
   ``require_console_tenant`` / ``require_console_tenant_write`` 里写一次（#82 修复前它
   在本模块又写了一遍，两套实现迟早会漂移）。四个服务函数从传进来的 ``ConsoleIdentity``
   取 ``tenant_id`` 与 ``account_id``（审计要后者）；漏掉门禁是路由的错，不是这里的第二道闸；
2. **跨租户按「不存在」处理**（``20016``）。Key / 配置的主键是全局唯一的裸字符串，
   「不属于你」与「不存在」对外必须是同一个答案，否则枚举一遍 id 就知道谁的 Key 在哪；
3. **改动与审计同一个事务**（与套餐 / 账号那两条写入口同一套收口）。签发会同时写
   ``api_keys`` 与 ``key_policies`` 两行，三条写入一起提交：不允许出现「Key 发出去了、
   配置没建」或「改了配置、账本里没有」的窗口。提交点不在这里，在路由声明的
   ``Depends(get_async_transaction)`` 上（见 :mod:`app.db.transaction`）。

自洽校验不在这里重写：它只有 :func:`app.services.profile_policy.ensure_policy_consistent`
一份，本模块只负责把它抛出的 ``AppError(20015)`` 与入参错误（``ValueError``）翻成控制面的
同一个码。

**整层都在异步栈上**（第 11 步票 #129 → #130）：会话是 ``AsyncSession``，签发时那三行
写入共用同一个会话、同一笔事务。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.console_auth import ConsoleIdentity
from app.core.exceptions import AppError, ErrorCode
from app.models.api_key import ApiKey
from app.models.key_policy import KeyPolicy
from app.repositories import api_key_repository, key_policy_repository
from app.services import api_key_service, console_service, key_policy_service
from app.utils.id_generator import mask_api_key_tail

#: 审计里的动作名。列表与筛选都按它读，所以写在一处。``key.`` 与 ``key_policy.`` 两个
#: 前缀让「动的是凭证还是配置」一眼可分——它们对客户的影响完全不同。
ACTION_KEY_ISSUE = "key.issue"
ACTION_KEY_DELETE = "key.delete"
#: 重新生成（轮换密钥本体）。与 `key.issue` 刻意分开：签发是**加法**（多一把 Key，旧的一把
#: 不受影响），重新生成是**替换**（同一把 Key 换凭证，旧明文进入过渡期）。混成一个动作名等于
#: 抹掉事故现场——事后没人能从审计里看出「这次到底是多发了一把，还是把在用的那把换了」。
ACTION_KEY_REGENERATE = "key.regenerate"
#: 把过渡密钥提前作废（不必等 24 小时窗口自己到期）。
ACTION_KEY_PREV_REVOKE = "key.prev_revoke"
ACTION_KEY_POLICY_UPDATE = "key_policy.update"

#: 配置的名字。请求体里只有 ``label``（给 Key 的备注名），配置名从它派生；没有 label 时
#: 用这个默认值。名字只给人看，不参与任何判定。
DEFAULT_POLICY_NAME = "default"


@dataclass(frozen=True)
class KeyRowView:
    """列表的一行：Key 行 + 它引用的配置行（配置不存在时为 ``None``）。"""

    api_key: ApiKey
    policy: KeyPolicy | None


@dataclass(frozen=True)
class IssuedKey:
    """一次签发的产物：刚写入的 Key 行 + 连带建出来的配置行 + **只此一次**的原文。

    ``plaintext`` 是这条路线上唯一的原文出口：行里只有哈希与末 5 位。路由把它放进签发
    响应，之后谁也取不回来。
    """

    api_key: ApiKey
    policy: KeyPolicy
    plaintext: str


@dataclass(frozen=True)
class RegeneratedKey:
    """一次重新生成的产物：原有的 Key 行（已换上新的密钥本体）+ **只此一次**的原文。

    ``prev_key_expires_at`` 一并交出去：路由要把它回给前端，界面才能说清「旧 Key 还能用到
    几点」。它不是新算的——就是这次写进那一列的值。
    """

    api_key: ApiKey
    policy: KeyPolicy | None
    plaintext: str
    prev_key_expires_at: datetime


@dataclass(frozen=True)
class DeletedKey:
    """一次删 Key 的产物：被删掉的那把 Key 的 id。"""

    key_id: str


@dataclass(frozen=True)
class PolicyPatchResult:
    """一次改配置的产物：改前 / 改后同一形状的快照，以及真变了的字段。"""

    policy_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    changed: tuple[str, ...]


def _window_closed(api_key: ApiKey, now: datetime) -> bool:
    """过渡窗口是不是已经关了（作废 / 到期同一个判据）。

    这个不等式必须与 :func:`app.core.auth._ensure_secret_matches` 里那条逐字一致——
    两处各写一遍是这一票最容易漂的地方：宽一秒就是「作废了但还能用」，严一秒就是
    「还没到期却先拒了」。所以它只在这里出现一次，鉴权那边读的也是同一个形状。
    """

    expires = api_key.prev_key_expires_at
    return expires is None or now > expires


def _verified_tenant_id(actor: ConsoleIdentity) -> str:
    """已校验身份 → 裸 ``tenant_id``（**只做类型收窄**，不再产生 ``20011``）。

    租户判定由 HTTP 依赖（``require_console_tenant`` / ``require_console_tenant_write``）
    完成；本模块是业务层，不该认识「HTTP 请求」这件事，也不该有第二套拒绝文案。
    """

    assert actor.tenant_id is not None, "租户门禁应已拒绝无租户的管理员"
    return actor.tenant_id


async def list_keys(db: AsyncSession, *, actor: ConsoleIdentity) -> list[KeyRowView]:
    """本租户的 Key 列表，每行带上它引用的配置。

    配置按**同一租户**读一遍再按 id 对上：一条 Key 指向别的租户的配置属于手修库造出来的
    坏数据，此时如实回 ``policy=None``，而不是把别人的配置回显给这个租户。

    两次读（Key 行、配置行）一起 ``await``：这是控制面唯一的 Key 列表查询。
    """

    tenant_id = _verified_tenant_id(actor)
    policies = {
        policy.id: policy for policy in await key_policy_repository.list_by_tenant(db, tenant_id)
    }
    return [
        KeyRowView(api_key=api_key, policy=policies.get(api_key.policy_id))
        for api_key in await api_key_repository.list_by_tenant(db, tenant_id)
    ]


async def issue_key(
    db: AsyncSession,
    *,
    actor: ConsoleIdentity,
    label: str | None,
    policy_values: dict[str, Any],
) -> IssuedKey:
    """签发：连带建一套配置，两行与一条审计同一个事务落定。

    配置的自洽校验（档位 ⊆ 配置 ⊆ 套餐、数值上限）在
    :func:`app.services.key_policy_service.create_key_policy` 里，**这里不重写**；本函数只把
    它抛出的 ``ValueError``（入参不对）与 ``LookupError``（租户 / 套餐缺失）翻成控制面的码。
    ``AppError(20015)``（配置不自洽）原样上抛，错误码本来就是对的。
    """

    tenant_id = _verified_tenant_id(actor)
    try:
        created = await key_policy_service.create_key_policy(
            db,
            tenant_id=tenant_id,
            name=_policy_name_of(label),
            values=policy_values,
        )
    except ValueError as exc:
        raise AppError(ErrorCode.PARAM_ERROR, str(exc)) from exc
    except LookupError as exc:
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc

    minted = await api_key_service.create_api_key_row_async(
        db, tenant_id=tenant_id, policy_id=created.policy.id, label=label
    )
    api_key = minted.api_key
    await console_service.append_audit(
        db,
        actor=actor,
        action=ACTION_KEY_ISSUE,
        target=f"key:{api_key.id}",
        before=None,
        after=_key_audit_snapshot(api_key, created.policy),
    )
    return IssuedKey(api_key=api_key, policy=created.policy, plaintext=minted.plaintext)


async def regenerate_key(
    db: AsyncSession, *, actor: ConsoleIdentity, key_id: str, now: datetime
) -> RegeneratedKey:
    """**重新生成同一把 Key 的密钥本体**（第 17 步 #227 现场五）。

    ## 这条路径为什么存在

    库里只有 ``scrypt`` 哈希：Key 丢了就取不回来，「我要一把新的」是唯一出路。但「一把新的」
    有两种做法——签发一把（新 ``key_id``、要重新配白名单与额度），或者**给同一把换凭证**。
    用户在丢 Key 的当口最不想再配一遍白名单，所以这里是后者：

    * ``key_id`` / ``label`` / ``policy_id`` / ``status`` **全部不动**（身份与配置都还是它）；
    * 旧哈希与旧末 5 位搬进过渡三列，截止时刻 = 现在 + ``PREV_KEY_TTL``；
    * 新哈希与新末 5 位落到原两列；``rotated_at`` 记这一次。

    ## 只保留最近一把过渡密钥

    24 小时内再重新生成一次时，上一把旧址被**直接覆盖**（等于同时作废）：留一串旧址等于同时
    允许一串凭证有效，与「一把 Key 只有一份有效密钥」相反。界面里那句「只保留最近一把」说的
    就是这一行。

    ## 回执

    返回**设置了新原文的行**与过渡截止时刻；``plaintext`` 是这条路线唯一的原文出口，
    路由把它放进响应，之后谁也取不回来。
    """

    tenant_id = _verified_tenant_id(actor)
    api_key = await api_key_repository.get_by_id(db, key_id)
    if api_key is None or api_key.tenant_id != tenant_id:
        # 与删除同一条口径：主键是全局唯一的裸字符串，「不属于你」与「不存在」必须是同一个答案。
        raise AppError(ErrorCode.NOT_FOUND, f"key not found: {key_id}")

    minted = api_key_service.mint_secret(key_id=api_key.id)
    before = _key_audit_snapshot(api_key, await _policy_of_key(db, api_key, tenant_id))

    # 旧址原样搬过去（不是重新哈希：搬的就是这一行里已经算好的值）。
    api_key.prev_key_hash = api_key.key_hash
    api_key.prev_key_tail = api_key.key_tail
    api_key.prev_key_expires_at = now + api_key_service.PREV_KEY_TTL
    api_key.key_hash = minted.key_hash
    api_key.key_tail = minted.key_tail
    api_key.rotated_at = now
    await db.flush()

    policy = await _policy_of_key(db, api_key, tenant_id)
    await console_service.append_audit(
        db,
        actor=actor,
        action=ACTION_KEY_REGENERATE,
        target=f"key:{api_key.id}",
        before=before,
        after=_key_audit_snapshot(api_key, policy),
    )
    return RegeneratedKey(
        api_key=api_key,
        policy=policy,
        plaintext=minted.plaintext,
        prev_key_expires_at=api_key.prev_key_expires_at,
    )


async def revoke_previous_secret(
    db: AsyncSession, *, actor: ConsoleIdentity, key_id: str, now: datetime
) -> bool:
    """把**过渡密钥**提前作废（不必等窗口自己到期）。

    返回「这次是否真的作废了一把」：没有过渡密钥时是 ``False`` 而不是报错——这条路径是
    **幂等**的，界面上的按钮只在窗口期内出现，但重试、双击、两个标签页同时点都不该变成
    一个错误码。审计也只在真作废时写一条（``key.prev_revoke``）：没作废到东西就没有可记的事。

    ## 为什么作废 = 「把窗口关到过去」，而不是第五个状态位

    三种写法都能让旧密钥失效：加一列 `prev_key_revoked_at`（多一个状态位，判定要
    `expires > now AND revoked_at IS NULL` 两个条件）、清空三列（**旧串再请求时就分不清
    「凭证换了」与「Key 不存在」**，而那正是运维换 Key 后最需要看到的那句话）、
    把 `prev_key_expires_at` 改成过去（判据仍然是**同一个**时刻）。

    这里选第三种：作废只是「提前到期」，于是自然到期与人为作废走**完全同一条**判定与
    同一句文案，界面也只需要 `过渡截止时刻 > 现在` 一个条件。旧哈希与末 5 位**留着**：
    它们已经过不了校验（判据是时刻），但留着才能在审计里讲清「这把 Key 的过渡期什么时候
    结束的」，并让旧串继续收到那句专用文案。

    ## 为什么用 `now - 1 秒` 而不是 `now`

    判据是 `now <= prev_key_expires_at`，写成 `now` 的话「作废那一瞬间它仍然可用」——
    一个不该存在的窗口。往前挪一秒让「作废」与「过期」在语义上完全对齐。
    """

    tenant_id = _verified_tenant_id(actor)
    api_key = await api_key_repository.get_by_id(db, key_id)
    if api_key is None or api_key.tenant_id != tenant_id:
        raise AppError(ErrorCode.NOT_FOUND, f"key not found: {key_id}")

    if api_key.prev_key_hash is None:
        return False
    if _window_closed(api_key, now):
        # 已经到期（自然过期或已经被作废过一次）：这次没有改变任何东西，也就不记账。
        return False

    before = _key_audit_snapshot(api_key, await _policy_of_key(db, api_key, tenant_id))
    api_key.prev_key_expires_at = now - timedelta(seconds=1)
    await db.flush()
    await console_service.append_audit(
        db,
        actor=actor,
        action=ACTION_KEY_PREV_REVOKE,
        target=f"key:{api_key.id}",
        before=before,
        after=_key_audit_snapshot(api_key, await _policy_of_key(db, api_key, tenant_id)),
    )
    return True


async def delete_key(db: AsyncSession, *, actor: ConsoleIdentity, key_id: str) -> DeletedKey:
    """删一把 Key：**配置行不动**（结构上配置独立于 Key，可能还被别的 Key 复用）。

    读一行、判租户、删一行、写一条审计、提交——判断顺序刻意是「先读后判」：Key 的主键是
    全局唯一的裸字符串，先判租户再读行会让「不属于你」与「不存在」在实现上分成两条路，
    而它们对外必须是同一个 ``20016``。
    """

    tenant_id = _verified_tenant_id(actor)
    api_key = await api_key_repository.get_by_id(db, key_id)
    if api_key is None or api_key.tenant_id != tenant_id:
        raise AppError(ErrorCode.NOT_FOUND, f"key not found: {key_id}")

    policy = await _policy_of_key(db, api_key, tenant_id)
    before = _key_audit_snapshot(api_key, policy)
    await api_key_repository.delete(db, api_key)
    await console_service.append_audit(
        db,
        actor=actor,
        action=ACTION_KEY_DELETE,
        target=f"key:{key_id}",
        before=before,
        after=None,
    )
    return DeletedKey(key_id=key_id)


async def _policy_of_key(db: AsyncSession, api_key: ApiKey, tenant_id: str) -> KeyPolicy | None:
    """Key 引用的配置；指向别的租户（手修库造出来的坏数据）时按「没有」处理。

    只用于审计快照：把别人的配置回显进这个租户的账本，等于把配置内容泄露出去。
    """

    policy = await key_policy_repository.get_by_id_async(db, api_key.policy_id)
    if policy is None or policy.tenant_id != tenant_id:
        return None
    return policy


async def patch_key_policy(
    db: AsyncSession,
    *,
    actor: ConsoleIdentity,
    policy_id: str,
    changes: dict[str, Any],
) -> PolicyPatchResult:
    """改一套配置（部分更新）：校验 → 落库 + 审计（同一个事务）。

    自洽校验只有一份实现，在 :func:`app.services.key_policy_service.apply_key_policy_update_async`
    里；本函数只负责「这个配置是不是你的」「把入参错误翻成 ``20015``」「补一条账本」。
    跨租户与不存在共用同一个 ``20016``：主键是全局唯一的裸字符串，两种情形对外必须同形，
    否则枚举 id 就能知道谁有哪些配置。
    """

    tenant_id = _verified_tenant_id(actor)
    policy = await key_policy_repository.get_by_id_async(db, policy_id)
    if policy is None or policy.tenant_id != tenant_id:
        raise AppError(ErrorCode.NOT_FOUND, f"key policy not found: {policy_id}")
    if not changes:
        raise AppError(ErrorCode.PARAM_ERROR, "no fields to change")

    try:
        update = await key_policy_service.apply_key_policy_update_async(
            db, policy_id=policy_id, changes=changes
        )
    except ValueError as exc:
        raise AppError(ErrorCode.PARAM_ERROR, str(exc)) from exc
    except LookupError as exc:
        # 上面刚读到了这一行；走到这里说明租户 / 套餐行缺失，属于坏数据，按不存在处理。
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc

    await console_service.append_audit(
        db,
        actor=actor,
        action=ACTION_KEY_POLICY_UPDATE,
        target=f"policy:{policy_id}",
        before=update.before,
        after=update.after,
    )
    return PolicyPatchResult(
        policy_id=update.policy_id,
        before=update.before,
        after=update.after,
        changed=update.changed,
    )


def _policy_name_of(label: str | None) -> str:
    """Key 的备注名 → 配置名；没给 label 时用 ``default``。"""

    if label is not None and label.strip():
        return label.strip()
    return DEFAULT_POLICY_NAME


def _key_audit_snapshot(api_key: ApiKey, policy: KeyPolicy | None) -> dict[str, Any]:
    """审计里的 Key 快照：**只留打码形态**，不留可用凭证。

    审计列表是管理员跨租户看的（``GET /console/audit-logs``）。改哈希之后这里连「要不要
    打码」都不必再判断——库里只剩 ``key_tail``，快照里能放的也只有它拼出来的那几个字符。
    ``policy`` 为 ``None``（配置缺失或指向别的租户）时如实回 ``null``，不编默认值。
    """

    return {
        "key_id": api_key.id,
        "tenant_id": api_key.tenant_id,
        "policy_id": api_key.policy_id,
        "label": api_key.label,
        "status": api_key.status,
        "key_masked": mask_api_key_tail(api_key.key_tail),
        # 三个只在「重新生成」之后才有值的字段：审计里能看出这次是不是轮换、窗口到几点。
        # 过渡密钥的**末 5 位**也放进来（同样是打码形态，不是可用凭证）。
        "prev_key_masked": (
            mask_api_key_tail(api_key.prev_key_tail)
            if api_key.prev_key_tail is not None
            else None
        ),
        "prev_key_expires_at": (
            api_key.prev_key_expires_at.isoformat()
            if api_key.prev_key_expires_at is not None
            else None
        ),
        "rotated_at": api_key.rotated_at.isoformat() if api_key.rotated_at is not None else None,
        "policy": (key_policy_service.policy_values_of(policy) if policy is not None else None),
    }
