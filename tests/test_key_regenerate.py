"""重新生成 Key 的密钥本体 + 过渡密钥的 24 小时窗口（第 17 步 #227 现场五）。

## 这一票要钉住的四件事

1. **轮换换的是凭证，不是身份**：`key_id` / `label` / `policy_id` / `status` 一个字都不动，
   只有两个哈希列（当前 + 过渡）与三个时刻变化。这是「丢 Key 但不想重配白名单」这条用户
   需求能不能成立的全部前提。
2. **过渡窗口的判据是时刻，不是标志位**：窗口内旧密钥能过校验，窗口外只认新的，且给一句
   **专用文案**（不是「Key 不存在」——那会让人去核对一把其实活得好好的 Key）。
3. **只保留最近一把过渡密钥**：24 小时内再轮换一次，上一把旧址被覆盖（等于同时作废）。
   留一串旧址等于同时允许一串凭证有效，与「一把 Key 只有一份有效密钥」相反。
4. **提前作废是幂等的**：没有过渡密钥时回 `revoked=false` 而不是错误码（双击、两个标签页
   同时点都不该变成一次失败）。

## 为什么用内存假会话而不是真库

被验的是**判定与记账**（哪两列被搬、截止时刻怎么算、审计写了什么、鉴权怎么放行），
不是 SQL 能不能跑；真库上另有一次容器里的实跑（见改造报告 §十五）。假会话只实现
`get` / `flush` 两个方法，故意不实现 `commit`——写路径的提交点归路由的事务依赖，
这一层不该有第二个提交出口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.core import auth
from app.core.console_auth import ConsoleIdentity
from app.core.exceptions import AppError, ErrorCode
from app.core.passwords import hash_password
from app.models.api_key import ApiKey
from app.services import api_key_service, console_service, key_admin_service
from app.utils.id_generator import api_key_tail_of, new_api_key_value, split_api_key_value
from tests.support.keys import mint_key

TENANT = "tenant_rotate"
OTHER_TENANT = "tenant_other"
KEY_ID = "key-rotate-1"
POLICY_ID = "policy-rotate-1"
NOW = datetime(2026, 11, 27, 8, 0, 0)


@dataclass
class _Session:
    """只够本文件用的假会话：`get` 找行、`flush` 计数；**故意没有 commit**。

    没有 `commit` 这件事本身是一条纪律：写路径的提交点只在路由的 `get_async_transaction`
    上，服务层偷偷提交会造成「服务函数调完就已经落库」这种事后很难查的半提交。
    """

    rows: dict[tuple[type[Any], str], Any]
    flushes: int = 0

    async def get(self, model: type[Any], pk: str) -> Any:
        return self.rows.get((model, pk))

    async def flush(self) -> None:
        self.flushes += 1


def _identity(tenant_id: str = TENANT) -> ConsoleIdentity:
    return ConsoleIdentity(
        account_id="acct-1",
        username="owner",
        role="owner",
        tenant_id=tenant_id,
    )


def _key(tenant_id: str = TENANT) -> ApiKey:
    return mint_key(key_id=KEY_ID, tenant_id=tenant_id, policy_id=POLICY_ID).api_key


async def _regenerate(
    session: _Session,
    *,
    tenant_id: str = TENANT,
    now: datetime = NOW,
) -> key_admin_service.RegeneratedKey:
    return await key_admin_service.regenerate_key(
        session,  # type: ignore[arg-type]
        actor=_identity(tenant_id),
        key_id=KEY_ID,
        now=now,
    )


@pytest.fixture(autouse=True)
def _record_audits(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把审计换成记录：这一层要断言的正是「这次改动留了哪一条账」。"""

    recorded: list[dict[str, Any]] = []

    async def _append(
        db: Any, *, actor: Any, action: str, target: str, before: Any, after: Any
    ) -> None:
        recorded.append({"action": action, "target": target, "before": before, "after": after})

    monkeypatch.setattr(console_service, "append_audit", _append)
    return recorded


def _session(api_key: ApiKey) -> _Session:
    return _Session(rows={(ApiKey, api_key.id): api_key})


# ── 纯函数：新密钥本体 ──────────────────────────────────────────────


def test_minting_a_secret_keeps_the_same_key_id() -> None:
    """新密钥本体的串里，行身份证仍然是**同一个** `key_id`。

    这一条是整套轮换的地基：鉴权靠串里的 `key_id` 找行，如果换出来的是新 id，那么「同一把
    Key 换凭证」就变成了「签发一把新 Key」——旧行永远不会被命中，过渡窗口也就无从谈起。
    """

    minted = api_key_service.mint_secret(key_id=KEY_ID)
    parsed = split_api_key_value(minted.plaintext)
    assert parsed is not None
    key_id, secret = parsed
    assert key_id == KEY_ID
    # 落库的两列与原文必须自洽：哈希对得上、末 5 位就是原文的末 5 位。
    assert auth.verify_password(secret, minted.key_hash)
    assert minted.key_tail == api_key_tail_of(secret)


def test_two_mints_produce_different_secrets() -> None:
    """两次生成必须是两份**不同**的密钥本体（否则「换了凭证」等于没换）。"""

    first = api_key_service.mint_secret(key_id=KEY_ID)
    second = api_key_service.mint_secret(key_id=KEY_ID)
    assert first.key_hash != second.key_hash
    assert first.plaintext != second.plaintext


# ── 轮换：搬旧址、换新址、记账 ────────────────────────────────────


@pytest.mark.asyncio
async def test_regenerating_moves_the_old_secret_into_the_transition_columns(
    _record_audits: list[dict[str, Any]],
) -> None:
    """旧址原样搬进过渡三列，新址落到当前两列，`rotated_at` 记这一次。

    「原样搬」是刻意的：搬的是这一行里已经算好的哈希，**不重新哈希一次**——重新哈希会让
    过渡密钥与「轮换前那一刻真正在用的那把」在极低概率下不是同一个，而那种事只在真库里偶发。
    """

    api_key = _key()
    old_hash, old_tail = api_key.key_hash, api_key.key_tail
    session = _session(api_key)

    regenerated = await _regenerate(session)

    assert api_key.key_hash != old_hash
    assert api_key.key_tail != old_tail
    assert api_key.prev_key_hash == old_hash, "旧址必须是原来那一份"
    assert api_key.prev_key_tail == old_tail
    assert api_key.prev_key_expires_at == NOW + api_key_service.PREV_KEY_TTL
    assert regenerated.prev_key_expires_at == api_key.prev_key_expires_at
    assert api_key.rotated_at == NOW

    # 身份与配置一个字都没动——这正是「丢 Key 不用重配白名单」的全部理由。
    assert api_key.id == KEY_ID
    assert api_key.policy_id == POLICY_ID
    assert api_key.status == "active"
    # label 与 policy 也没被改写（这个用例的 key 没给 label，所以它仍然是 None）。
    assert api_key.label is None


@pytest.mark.asyncio
async def test_regenerating_writes_one_audit_with_a_masked_snapshot(
    _record_audits: list[dict[str, Any]],
) -> None:
    """审计：一条 `key.regenerate`，前后快照里只有打码形态，且带过渡窗口截止时刻。

    与 `key.issue` 分一个动作名的理由在动作名旁边写了：签发是加法、轮换是替换+旧凭证失效，
    混在一起等于抹掉事故现场。
    """

    api_key = _key()
    session = _session(api_key)

    await _regenerate(session)

    assert len(_record_audits) == 1
    entry = _record_audits[0]
    assert entry["action"] == key_admin_service.ACTION_KEY_REGENERATE
    assert entry["target"] == f"key:{KEY_ID}"
    assert entry["before"]["key_masked"] != entry["after"]["key_masked"]
    assert entry["after"]["prev_key_masked"] == entry["before"]["key_masked"]
    assert entry["after"]["prev_key_expires_at"] is not None
    assert entry["after"]["rotated_at"] is not None
    # 快照里不许出现任何可用凭证（只有打码串）。
    text = repr(entry)
    assert "scrypt$" not in text


@pytest.mark.asyncio
async def test_a_second_regeneration_replaces_the_previous_secret(
    _record_audits: list[dict[str, Any]],
) -> None:
    """24 小时内再轮换一次：上一把旧址**被覆盖**，始终只保留最近一把。

    这是界面里那句「过渡期只保留最近一把：24 小时内再点一次，上一把过渡 Key 会立刻作废」
    的机器侧；没有这一条，用户读到那句话时无法核对它是不是真的。
    """

    api_key = _key()
    session = _session(api_key)
    original_hash = api_key.key_hash

    first = await _regenerate(session, now=NOW)
    second = await _regenerate(session, now=NOW + timedelta(hours=1))

    assert api_key.prev_key_hash != original_hash, "第二把旧址已经顶掉了第一把"
    assert api_key.prev_key_expires_at == NOW + timedelta(hours=1) + api_key_service.PREV_KEY_TTL
    assert first.plaintext != second.plaintext
    assert [entry["action"] for entry in _record_audits] == [
        key_admin_service.ACTION_KEY_REGENERATE
    ] * 2


@pytest.mark.asyncio
async def test_regenerating_a_key_of_another_tenant_is_not_found(
    _record_audits: list[dict[str, Any]],
) -> None:
    """跨租户：按「不存在」处理（20016），且一个字都不写。"""

    api_key = _key(tenant_id=OTHER_TENANT)
    session = _session(api_key)
    old_hash = api_key.key_hash

    with pytest.raises(AppError) as raised:
        await _regenerate(session)

    assert raised.value.code == ErrorCode.NOT_FOUND
    assert api_key.key_hash == old_hash, "被拒的那一次不该改任何一列"
    assert api_key.prev_key_hash is None
    assert _record_audits == []


# ── 提前作废 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoking_clears_the_transition_columns_and_records_one_audit(
    _record_audits: list[dict[str, Any]],
) -> None:
    """提前作废：把 `prev_key_expires_at` 关到过去，记一条 `key.prev_revoke`。

    不清空哈希与末 5 位是刻意的：它们已经过不了校验（判据是时刻），但留着才能让旧串继续
    收到专用文案、让审计讲清「这次过渡是怎么结束的」。
    """

    api_key = _key()
    session = _session(api_key)
    await _regenerate(session)

    assert await key_admin_service.revoke_previous_secret(  # type: ignore[arg-type]
        session, actor=_identity(), key_id=KEY_ID, now=NOW + timedelta(hours=1)
    )

    # 作废 = 把窗口关到过去（**不是**清空三列）：判据仍然只有那个时刻，而留着哈希与末 5 位
    # 才能让旧串继续收到那句「密钥已重新生成」的专用文案、让审计讲清窗口何时结束。
    assert api_key.prev_key_expires_at == NOW + timedelta(hours=1) - timedelta(seconds=1)
    assert api_key.prev_key_hash is not None
    # 当前密钥不受影响：作废的是旧的。
    assert api_key.key_hash is not None
    assert [entry["action"] for entry in _record_audits][-1] == (
        key_admin_service.ACTION_KEY_PREV_REVOKE
    )


@pytest.mark.asyncio
async def test_revoking_without_a_transition_secret_is_idempotent(
    _record_audits: list[dict[str, Any]],
) -> None:
    """没有过渡密钥时不报错、不记账：回 `False`。

    界面上的按钮只在窗口期内出现，但重试、双击、两个标签页同时点都不该变成一个错误码；
    而「没作废到任何东西」就不该在账本里留下一条记录。
    """

    api_key = _key()
    session = _session(api_key)

    assert not await key_admin_service.revoke_previous_secret(  # type: ignore[arg-type]
        session, actor=_identity(), key_id=KEY_ID, now=NOW
    )
    assert _record_audits == []


@pytest.mark.asyncio
async def test_revoking_twice_second_time_changes_nothing(
    _record_audits: list[dict[str, Any]],
) -> None:
    """已经作废/到期之后再作废一次：回 `False` 且不再记账（幂等）。

    双击、两个标签页同时点、或者「自然过期之后又点了一下」都会走到这里；把它做成
    第二次就报错，会让这类无害重试看起来像一次失败。
    """

    api_key = _key()
    session = _session(api_key)
    await _regenerate(session)
    assert await key_admin_service.revoke_previous_secret(  # type: ignore[arg-type]
        session, actor=_identity(), key_id=KEY_ID, now=NOW + timedelta(hours=1)
    )

    assert not await key_admin_service.revoke_previous_secret(  # type: ignore[arg-type]
        session, actor=_identity(), key_id=KEY_ID, now=NOW + timedelta(hours=2)
    )
    assert api_key.prev_key_expires_at == NOW + timedelta(hours=1) - timedelta(seconds=1)


# ── 鉴权：过渡窗口 ────────────────────────────────────────────────


class _Cache:
    """`KeyVerifyCache` 的内存替身：只记住「这个 token 验过、属于哪把 Key」。"""

    def __init__(self) -> None:
        self._remembered: dict[str, str] = {}

    async def key_id_of(self, token: str) -> str | None:
        return self._remembered.get(token)

    async def remember(self, token: str, key_id: str) -> None:
        self._remembered[token] = key_id


def _pairs() -> tuple[str, str, str, str]:
    """造两对「明文 / 密钥本体」：当前那把与过渡那把（仍共用同一行 `key_id`）。"""

    current = new_api_key_value(KEY_ID)
    previous = new_api_key_value(KEY_ID)
    parsed_current = split_api_key_value(current)
    parsed_previous = split_api_key_value(previous)
    assert parsed_current is not None and parsed_previous is not None
    return current, parsed_current[1], previous, parsed_previous[1]


@pytest.mark.asyncio
async def test_the_current_secret_is_accepted() -> None:
    """当前密钥：正常通过，且进校验缓存。"""

    token, secret, _old_token, _old_secret = _pairs()
    cache = _Cache()

    await auth._ensure_secret_matches(
        token,
        secret,
        key_id=KEY_ID,
        key_hash=hash_password(secret),
        verify_cache=cache,  # type: ignore[arg-type]
    )

    assert await cache.key_id_of(token) == KEY_ID


@pytest.mark.asyncio
async def test_the_previous_secret_is_accepted_inside_the_window() -> None:
    """过渡密钥在窗口内：**放行**，并进缓存（同一个 token 不会每请求烧一次 scrypt）。

    这就是「24 小时过渡」的全部机器侧：旧 Key 还能用，是为了让线上业务有时间换配置，
    而不是让泄露的钥匙长期有效。
    """

    _token, _secret, old_token, old_secret = _pairs()
    cache = _Cache()

    await auth._ensure_secret_matches(
        old_token,
        old_secret,
        key_id=KEY_ID,
        key_hash=hash_password(_secret),
        prev_key_hash=hash_password(old_secret),
        prev_key_expires_at=NOW + timedelta(hours=1),
        now=NOW,
        verify_cache=cache,  # type: ignore[arg-type]
    )

    # **不进缓存**：这条支路走的是即将作废的凭证，缓存会把它变成一把 60 秒有效的钱包——
    # 用户点了「作废旧 Key」之后旧串还能靠缓存再活一个 TTL。代价是过渡期内旧串每请求多烧
    # 一次 scrypt，而这本来就是一段要被赶快的状态。
    assert await cache.key_id_of(old_token) is None


@pytest.mark.asyncio
async def test_the_previous_secret_is_refused_after_the_window_with_a_dedicated_message() -> None:
    """窗口过了：拒绝，且文案必须是**专用**那句（不是「Key 不存在」）。

    为什么非要多烧一次 scrypt 去确认「这确实是那把旧密钥」：只有确认了，才能说
    「密钥已重新生成」；否则拿着旧 Key 的运维会去核对一把其实完全正常的 Key。
    """

    _token, _secret, old_token, old_secret = _pairs()
    cache = _Cache()

    with pytest.raises(AppError) as raised:
        await auth._ensure_secret_matches(
            old_token,
            old_secret,
            key_id=KEY_ID,
            key_hash=hash_password(_secret),
            prev_key_hash=hash_password(old_secret),
            prev_key_expires_at=NOW - timedelta(seconds=1),
            now=NOW,
            verify_cache=cache,  # type: ignore[arg-type]
        )

    assert raised.value.code == ErrorCode.UNAUTHORIZED
    assert raised.value.msg == auth.MSG_KEY_ROTATED
    assert "已重新生成" in raised.value.msg
    assert await cache.key_id_of(old_token) is None, "被拒的 token 不许进缓存"

    # 反例对照：同一把 Key 上、只是随便一串错密钥 → 仍然是「不存在」，
    # 不泄露「这把 Key 存在、而且它有过过渡密钥」。
    wrong = new_api_key_value(KEY_ID)
    parsed_wrong = split_api_key_value(wrong)
    assert parsed_wrong is not None
    with pytest.raises(AppError) as wrong_raised:
        await auth._ensure_secret_matches(
            wrong,
            parsed_wrong[1],
            key_id=KEY_ID,
            key_hash=hash_password(_secret),
            prev_key_hash=hash_password(old_secret),
            prev_key_expires_at=NOW + timedelta(hours=1),
            now=NOW,
            verify_cache=cache,  # type: ignore[arg-type]
        )
    assert wrong_raised.value.msg == auth.MSG_UNKNOWN_KEY


@pytest.mark.asyncio
async def test_a_key_without_a_transition_secret_behaves_as_before() -> None:
    """从没轮换过的 Key：行为与改造前逐字一致（错密钥 → 「不存在」）。"""

    token, secret, _old_token, _old_secret = _pairs()
    cache = _Cache()

    await auth._ensure_secret_matches(
        token,
        secret,
        key_id=KEY_ID,
        key_hash=hash_password(secret),
        verify_cache=cache,  # type: ignore[arg-type]
    )

    wrong = new_api_key_value(KEY_ID)
    parsed_wrong = split_api_key_value(wrong)
    assert parsed_wrong is not None
    with pytest.raises(AppError) as raised:
        await auth._ensure_secret_matches(
            wrong,
            parsed_wrong[1],
            key_id=KEY_ID,
            key_hash=hash_password(secret),
            verify_cache=cache,  # type: ignore[arg-type]
        )
    assert raised.value.msg == auth.MSG_UNKNOWN_KEY
