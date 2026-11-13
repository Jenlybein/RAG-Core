"""票 #93：配置中台的纯逻辑验收（三档白名单、热更落实、非法行回退、影响预览）。

接缝只有两个：:mod:`app.services.runtime_settings` 的公开函数，以及
``app.core.config.settings`` 这个进程级单例。数据库换成本仓既有的
``RecordingSession``，缓存换成本文件里的内存假实现——**不连真库、不连 Redis**。

这一组刻意不碰 HTTP：热更的语义（「下一次调用按新值跑」）在服务层就能判定，而把每个
用例都塞进 ``TestClient`` 只会让失败信息变成「某个响应码不对」，看不出到底是合并错了
还是路由绑错了。端点那一组在 ``tests/test_console_settings.py``。
"""

from __future__ import annotations

import asyncio

from datetime import datetime
from typing import Any

import pytest

from app.core.config import settings
from app.db.transaction import async_transaction
from app.core.exceptions import AppError, ErrorCode
from app.core.runtime_settings_cache import (
    reset_settings_cache,
    set_settings_cache,
)
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_PRESETS, Plan, plan_row_of
from app.models.runtime_setting import RuntimeSetting
from app.models.tenant import Tenant
from app.services import runtime_settings
from app.services.runtime_settings import (
    SETTING_SPECS,
    TIER_HOT,
    TIER_LOCKED,
    TIER_RESTART,
    TIERS,
    TARGET_POINTER,
    describe_settings,
    editable_specs,
    spec_of,
)
from tests.fake_db import AsyncRecordingSession, RecordingSession

SAFETY_SWITCHES = ("AUTH_ENABLED", "RATE_LIMIT_ENABLED", "DEV_API_KEY", "DEV_TENANT_ID")
ADMIN_ID = "00000000-0000-4000-8000-000000000001"

#: 库里预置一行时用的时间戳。``updated_at`` 是否被改写，靠它看出来。
SEEDED_AT = datetime(2026, 1, 1, 8, 0, 0)


class _MemorySettingsCache:
    """内存假缓存：``get/set/delete`` 三个方法就是全部接缝（与 ``plan_cache`` 同形）。"""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.set_calls: int = 0
        self.deleted: int = 0

    def get(self) -> dict[str, Any] | None:
        return None if self.payload is None else dict(self.payload)

    def set(self, overrides: dict[str, Any]) -> None:
        self.set_calls += 1
        self.payload = dict(overrides)

    def delete(self) -> None:
        self.deleted += 1
        self.payload = None

    async def adelete(self) -> None:
        """循环上的那条：与 ``delete`` 同一份状态（契约见 app.core.runtime_settings_cache）。"""

        self.delete()


class _ExplodingSession(RecordingSession):
    """任何查询都炸：用来证明「命中缓存 / TTL 内的 ensure 一次库都没读」。"""

    def query(self, _model: type) -> Any:  # pragma: no cover - 访问即失败
        raise AssertionError("这条路径不该读数据库")

    def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("这条路径不该读数据库")


def _row(key: str, value: Any, *, updated_at: datetime | None = None) -> RuntimeSetting:
    return RuntimeSetting(
        key=key,
        value=value,
        updated_by=ADMIN_ID,
        updated_at=updated_at or SEEDED_AT,
    )


def _settings_rows(db: Any) -> list[Any]:
    """``describe_settings`` 自票 #129 起是协程（控制面只读走异步会话）。

    传进来的本来就是**异步替身**（见 :func:`_session`），直接 ``await`` 即可；这里用
    ``asyncio.run`` 只是为了让读面板的那些同步用例继续同步——它们是纯读，没有循环可复用。
    """

    return asyncio.run(describe_settings(db))


def _session(*rows: RuntimeSetting, world: dict[type, list[Any]] | None = None) -> Any:
    """异步替身：票 #130 起配置写路径（``patch_runtime_settings`` / ``preview_impact``）
    读的是 ``AsyncSession``。

    行与记录仍由 ``RecordingSession`` 提供的那一份语义负责（``AsyncRecordingSession``
    只是把同一组方法变成可 ``await``），所以 ``db.added_of(...)`` / ``db.commits`` 这些
    断言一行都不用改。
    """

    return AsyncRecordingSession(rows={**(world or {}), RuntimeSetting: list(rows)})


def _actor() -> Any:
    """服务层眼里的「谁」：只需要 ``account_id``，所以直接用仓库里真实的判定结论对象。"""

    return runtime_settings_actor()


def runtime_settings_actor() -> Any:
    from app.core.console_auth import ConsoleIdentity

    return ConsoleIdentity(account_id=ADMIN_ID, username="admin", role="admin", tenant_id="t1")


def runtime_setting_row(db: Any, key: str) -> RuntimeSetting:
    return next(row for row in db._rows[RuntimeSetting] if row.key == key)


@pytest.fixture(autouse=True)
def _isolated_runtime() -> Any:
    """每个用例一套全新的进程内状态：配置单例、刷新状态、缓存、会话工厂全部复位。"""

    saved = settings.model_dump()
    runtime_settings.reset_runtime_state()
    set_settings_cache(_MemorySettingsCache())
    runtime_settings.set_session_factory(None)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)
        runtime_settings.reset_runtime_state()
        runtime_settings.set_session_factory(None)
        reset_settings_cache()


@pytest.fixture
def cache() -> _MemorySettingsCache:
    """本用例正在使用的内存缓存（与注入进进程的那个是同一个对象）。"""

    mem = _MemorySettingsCache()
    set_settings_cache(mem)
    return mem


# ────────────────────────────────────────────────────────────────── 三档与白名单


def test_the_whitelist_has_entries_in_all_three_tiers() -> None:
    """三档不能有一档是空的：空档意味着「面板上这一组什么都没有」，那本身就是个错误。"""

    assert TIERS == (TIER_LOCKED, TIER_RESTART, TIER_HOT)
    for tier in TIERS:
        assert editable_specs(tier), f"{tier} 档没有任何键"


def test_the_four_safety_switches_are_whitelisted_but_locked() -> None:
    """安全开关必须在白名单里（面板要展示并写明理由），但 tier 必须是 LOCKED。

    这条是本票最容易「做好事做坏」的地方：把它们放进白名单是为了让管理员**看见**
    「鉴权开关在哪里、为什么不能从面板改」；而一旦它们可写，一个被拿下的管理员账号
    就能关掉全站的鉴权。
    """

    for key in SAFETY_SWITCHES:
        spec = spec_of(key)
        assert spec is not None, key
        assert spec.tier == TIER_LOCKED, key
        assert spec.why.strip(), f"{key} 必须写明「为什么不能改」"


def test_locked_keys_are_never_editable() -> None:
    locked = editable_specs(TIER_LOCKED)
    assert set(locked) >= set(SAFETY_SWITCHES)
    assert set(locked).isdisjoint(editable_specs(TIER_HOT))


def test_every_whitelisted_key_really_exists_on_settings() -> None:
    """白名单与 ``Settings`` 不能分叉：指向一个不存在字段的键永远合并不进去。"""

    for key, spec in SETTING_SPECS.items():
        if spec.target == TARGET_POINTER:
            assert "." in key, f"{key}: 指针键应该带命名空间前缀"
            continue
        assert key in type(settings).model_fields, f"{key} 不在 Settings 上"


def test_every_whitelisted_key_explains_why_it_is_in_that_tier() -> None:
    """``why`` 是给 #96 直接展示的一栏人话；空着就只剩一个没人看得懂的档位名。"""

    for key, spec in SETTING_SPECS.items():
        assert len(spec.why.strip()) >= 10, f"{key} 的 why 太短，说明不了为什么在这一档"


def test_no_secret_is_whitelisted_as_hot() -> None:
    """密钥类字段不该出现在热更档：热更意味着值会被反复读进内存并可能被面板回显。"""

    secrets = [key for key, spec in SETTING_SPECS.items() if spec.secret]
    assert secrets == ["MODEL_API_KEY", "RERANK_API_KEY"]
    # 逐键钉死档位：写成「restart 或 locked 都行」等于这一档什么都守不住。
    assert spec_of("MODEL_API_KEY").tier == TIER_RESTART
    assert spec_of("RERANK_API_KEY").tier == TIER_LOCKED


def test_the_rerank_endpoint_keys_are_in_the_catalog() -> None:
    """票 #116：端点地址与模型名可改（热档），密钥类只读且不回显。

    为什么地址 / 模型名必须能在面板上改：它们是「这一台机器用哪个重排端点」的全部事实，
    调整一次不该要求重起整个 API 进程。而密钥不进热更档——它只该由部署方在 .env 里定，
    面板上看到的永远是打码值（与 ``MODEL_API_KEY`` 同一条纪律）。
    """

    base_url = spec_of("RERANK_BASE_URL")
    model = spec_of("RERANK_MODEL")
    api_key = spec_of("RERANK_API_KEY")

    assert base_url is not None and base_url.tier == TIER_HOT
    assert model is not None and model.tier == TIER_HOT
    assert base_url.why.strip() and model.why.strip()

    assert api_key is not None and api_key.secret is True
    assert api_key.tier == TIER_LOCKED, "密钥类只读：写入口一律拒绝"
    shown = runtime_settings._visible_value(api_key, "sk-rerank-0123456789")
    assert "sk-rerank-0123456789" != shown
    assert "0123456789" not in shown, "不回显：面板只看得到打码值"


# ─────────────────────────────────────────────────────────────── 非法行回退与校验


def test_an_illegal_value_falls_back_to_the_env_value_and_is_flagged(cache: Any) -> None:
    """库里写 ``-1``：读时回退 `.env` 值、该行标红、进程不崩。

    负的超时在 pydantic 眼里完全合法（字段就是 ``float``），没有白名单声明的下界时它会
    静默生效，然后每一次重排都「超时失败」——等于把重排能力关掉了却没有任何提示。
    """

    db = _session(_row("RERANK_TIMEOUT_SECONDS", -1))
    env_value = runtime_settings.ENV_BASELINE["RERANK_TIMEOUT_SECONDS"]

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert "RERANK_TIMEOUT_SECONDS" in report.invalid
    assert "RERANK_TIMEOUT_SECONDS" not in report.valid
    assert settings.RERANK_TIMEOUT_SECONDS == env_value, "非法行必须回退 .env 值"
    row = next(r for r in _settings_rows(db) if r.key == "RERANK_TIMEOUT_SECONDS")
    assert row.invalid is True
    assert row.source == "env"
    assert row.value == env_value
    assert row.invalid_reason and "≥" in row.invalid_reason


def test_a_valid_override_replaces_the_env_value(cache: Any) -> None:
    db = _session(_row("RERANK_TIMEOUT_SECONDS", 20.0))

    runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert settings.RERANK_TIMEOUT_SECONDS == 20.0
    row = next(r for r in _settings_rows(db) if r.key == "RERANK_TIMEOUT_SECONDS")
    assert (row.source, row.invalid, row.value) == ("db", False, 20.0)


def test_the_settings_validators_really_run_on_read(cache: Any) -> None:
    """写 ``RETRIEVAL_MODE="bing"``：该行被丢弃，而不是被静默采纳。

    这正是「就地 setattr 会绕过 validator」那个洞的检测点：``Settings`` 没开
    ``validate_assignment``，如果这里不显式跑一次 ``model_validate``，``settings``
    就会带着一个拼错的模式值继续服务。
    """

    db = _session(_row("RETRIEVAL_MODE", "bing"))

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert "RETRIEVAL_MODE" in report.invalid
    assert settings.RETRIEVAL_MODE == runtime_settings.ENV_BASELINE["RETRIEVAL_MODE"]


def test_a_validator_normalised_value_is_what_gets_applied(cache: Any) -> None:
    """``"Hybrid"`` 是合法输入（validator 会转小写），落进单例的必须是规范化的值。"""

    db = _session(_row("RETRIEVAL_MODE", "Hybrid"))

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert report.valid["RETRIEVAL_MODE"] == "hybrid"
    assert settings.RETRIEVAL_MODE == "hybrid"


def test_one_bad_row_does_not_discard_the_other_rows(cache: Any) -> None:
    """逐键校验的意义就在这里：坏的那一行只丢自己，不连带丢掉别人配好的值。"""

    db = _session(
        _row("RERANK_TIMEOUT_SECONDS", -1),
        _row("TOP_K", 9),
    )

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert set(report.invalid) == {"RERANK_TIMEOUT_SECONDS"}
    assert settings.TOP_K == 9
    assert (
        settings.RERANK_TIMEOUT_SECONDS == runtime_settings.ENV_BASELINE["RERANK_TIMEOUT_SECONDS"]
    )


def test_a_boolean_typed_string_is_rejected_instead_of_coerced(cache: Any) -> None:
    """``"false"`` 不许被当成 ``True``：JSONB 里手写成字符串是最常见的静默错误。"""

    db = _session(_row("RERANK_ENABLED", "false"))

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert "RERANK_ENABLED" in report.invalid
    assert settings.RERANK_ENABLED == runtime_settings.ENV_BASELINE["RERANK_ENABLED"]


def test_a_locked_key_in_the_database_is_ignored(
    cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绕过接口直接把 ``AUTH_ENABLED=false`` 写进库：进程**不许**照做。

    这是「能关掉鉴权的面板本身就是漏洞」那条纪律的另一半：面板不给写，库里的脏行
    也不能被采纳。它同时验证了三档是**代码里的决定**，不是库里的数据说了算。
    """

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    db = _session(_row("AUTH_ENABLED", False))

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert settings.AUTH_ENABLED is True, "库里的安全开关不允许生效"
    assert "AUTH_ENABLED" in report.invalid
    row = next(r for r in _settings_rows(db) if r.key == "AUTH_ENABLED")
    assert (row.editable, row.source, row.invalid) == (False, "env", True)


def test_an_unknown_key_in_the_database_is_ignored(cache: Any) -> None:
    """白名单外的键（哪怕有人手工塞进库）不参与合并，也不会让整批失效。"""

    db = _session(_row("NOT_A_SETTING", 1), _row("TOP_K", 7))

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert "NOT_A_SETTING" in report.invalid
    assert settings.TOP_K == 7


def test_pointer_keys_are_whitelisted_but_not_merged_into_settings(cache: Any) -> None:
    """``model.role.fast`` 只被业务代码读，``Settings`` 上没有对应字段。"""

    db = _session(_row("model.role.fast", "config-1"))

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert report.valid["model.role.fast"] == "config-1"
    assert "model.role.fast" not in report.merge_values(include_restart=True)
    assert not hasattr(settings, "model.role.fast")


# ──────────────────────────────────────────────────────────────────────── 回归红线


def test_no_rows_at_all_changes_nothing(monkeypatch: pytest.MonkeyPatch, cache: Any) -> None:
    """库里一行都没有时，进程行为与现状**逐字一致**。

    刻意先把一个键改成「不可能是缺省值」的值再跑合并：如果实现偷懒地「把整份
    ``Settings`` 按缺省写回去」，这个值会被抹掉——而那种写法会让运行期别人（测试、
    运维脚本）改过的属性在 30 秒后被悄悄还原。
    """

    monkeypatch.setattr(settings, "TOP_K", 77)
    db = _session()

    report = runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert report.valid == {}
    assert settings.TOP_K == 77


def test_the_singleton_is_mutated_in_place_so_import_time_references_see_it(cache: Any) -> None:
    """不许替换 ``app.core.config.settings`` 这个模块属性。

    全仓上百处 ``settings.X`` 都是 import 期拿到的对象引用；换掉属性之后，它们看到的
    还是旧对象，于是「配置改完下一次调用按新值」这条验收在那些模块里根本不成立。
    """

    from app.services import retrieval_options

    db = _session(_row("RERANK_TIMEOUT_SECONDS", 20.0))

    runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert retrieval_options.settings is settings
    assert retrieval_options.settings.RERANK_TIMEOUT_SECONDS == 20.0


def test_removing_an_override_restores_the_env_value(cache: Any) -> None:
    """覆盖被删掉之后要回到 ``.env`` 值，而不是把上一次的覆盖值永远留在单例里。

    删行的人（运维脚本）改完之后走同一条写路径删缓存；这里显式调用 ``invalidate``
    来模拟那一步，否则下一次读会命中 30 秒窗口内的旧缓存（那是设计行为，不是 bug）。
    """

    runtime_settings.apply_runtime_overrides(_session(_row("TOP_K", 9)), cache=cache)
    assert settings.TOP_K == 9

    runtime_settings.invalidate_runtime_settings(cache)
    runtime_settings.apply_runtime_overrides(_session(), cache=cache)

    assert settings.TOP_K == runtime_settings.ENV_BASELINE["TOP_K"]


def test_restart_tier_is_read_once_at_startup_and_not_hot_reloaded(cache: Any) -> None:
    """``CHUNK_SIZE`` 这类值改了要「重启 + 重建」，不能因为一次热刷新就悄悄生效。

    第一轮（本进程第一次合并）把它读进来；之后库里的行再变，热刷新不采纳它——因为已落库
    的切片是按旧窗口切的，热更会让同一个库里出现两套窗口。
    """

    runtime_settings.apply_runtime_overrides(_session(_row("CHUNK_SIZE", 256)), cache=cache)
    assert settings.CHUNK_SIZE == 256

    runtime_settings.invalidate_runtime_settings(cache)
    runtime_settings.apply_runtime_overrides(_session(_row("CHUNK_SIZE", 300)), cache=cache)

    assert settings.CHUNK_SIZE == 256, "第二档不参与热刷新"


# ──────────────────────────────────────────────────────────────────────── 缓存与传播


def test_a_cache_hit_never_touches_the_database(cache: Any) -> None:
    """缓存命中时一次库都不读；缓存里的非法值同样要过一遍校验。"""

    cache.payload = {"TOP_K": 8}

    report = runtime_settings.apply_runtime_overrides(_ExplodingSession(), cache=cache)

    assert report.valid["TOP_K"] == 8
    assert settings.TOP_K == 8


def test_a_cache_miss_reads_the_database_once_and_fills_the_cache(cache: Any) -> None:
    db = _session(_row("TOP_K", 8))

    runtime_settings.apply_runtime_overrides(db, cache=cache)

    assert cache.payload == {"TOP_K": 8}
    assert cache.set_calls == 1


def test_ensure_within_the_ttl_only_compares_time(cache: Any) -> None:
    """TTL 之内，``ensure`` 只做一次时间比较：不读缓存、不读库。"""

    runtime_settings.apply_runtime_overrides(_session(_row("TOP_K", 8)), cache=cache)
    cache.payload = None  # 即便缓存被清空，TTL 内也不该回源

    runtime_settings.ensure_runtime_overrides(_ExplodingSession(), cache=cache)

    assert settings.TOP_K == 8


# ────────────────────────────────────────────────────────────── 写入：dry_run 与白名单


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_still_returns_the_impact(cache: Any, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TOP_K", 5)
    db = _session()

    result = await runtime_settings.patch_runtime_settings(
        db, values={"TOP_K": 9}, dry_run=True, actor=_actor(), cache=cache
    )

    assert result.dry_run is True
    assert result.updated == ()
    assert result.impact is not None
    assert db.added_of(RuntimeSetting) == []
    assert db.commits == 0
    assert cache.deleted == 0


@pytest.mark.asyncio
async def test_dry_run_leaves_updated_at_untouched(cache: Any) -> None:
    """试算之后再去读那一行：``updated_at`` 必须还是原来那个时间。"""

    db = _session(_row("TOP_K", 5))
    before = runtime_setting_row(db, "TOP_K").updated_at

    await runtime_settings.patch_runtime_settings(
        db, values={"TOP_K": 9}, dry_run=True, actor=_actor(), cache=cache
    )

    assert runtime_setting_row(db, "TOP_K").updated_at == before
    assert db.commits == 0


@pytest.mark.asyncio
async def test_a_key_outside_the_whitelist_is_rejected_whole_batch(cache: Any) -> None:
    db = _session(_row("TOP_K", 5))

    with pytest.raises(AppError) as raised:
        await runtime_settings.patch_runtime_settings(
            db,
            values={"TOP_K": 9, "NOT_A_SETTING": 1},
            dry_run=False,
            actor=_actor(),
            cache=cache,
        )

    assert raised.value.code == ErrorCode.PARAM_ERROR
    assert db.commits == 0
    assert runtime_setting_row(db, "TOP_K").value == 5, "整批不写：连合法的那个键也没写"


@pytest.mark.asyncio
async def test_a_locked_key_is_rejected(cache: Any) -> None:
    with pytest.raises(AppError) as raised:
        await runtime_settings.patch_runtime_settings(
            db=_session(),
            values={"AUTH_ENABLED": False},
            dry_run=False,
            actor=_actor(),
            cache=cache,
        )

    assert raised.value.code == ErrorCode.PARAM_ERROR
    assert "AUTH_ENABLED" in raised.value.msg
    assert "原因" not in raised.value.msg  # 提示是「理由」，不是码名


@pytest.mark.asyncio
async def test_an_illegal_value_is_rejected_before_anything_is_written(cache: Any) -> None:
    with pytest.raises(AppError) as raised:
        await runtime_settings.patch_runtime_settings(
            db=_session(),
            values={"RERANK_TIMEOUT_SECONDS": -1},
            dry_run=False,
            actor=_actor(),
            cache=cache,
        )

    assert raised.value.code == ErrorCode.PARAM_ERROR
    assert "≥" in raised.value.msg


@pytest.mark.asyncio
async def test_a_real_write_persists_audits_and_deletes_the_cache(cache: Any) -> None:
    db = _session(_row("TOP_K", 5))

    # 非请求边界：直接调服务要自己圈事务（见 app/db/transaction.py 的约定）。会话是异步的，
    # 于是边界也是异步孪生——提交点仍然只有 `app.db.transaction` 那一处。
    async with async_transaction(db):
        result = await runtime_settings.patch_runtime_settings(
            db, values={"TOP_K": 9}, dry_run=False, actor=_actor(), cache=cache
        )

    assert result.dry_run is False
    assert result.updated == ("TOP_K",)
    assert result.hot == ("TOP_K",)
    assert result.before == {"TOP_K": 5}
    assert result.after == {"TOP_K": 9}
    assert "30s" in result.note, "面板必须如实写「各进程 30s 内生效」，不能写「立即」"

    assert runtime_setting_row(db, "TOP_K").value == 9
    assert runtime_setting_row(db, "TOP_K").updated_by == ADMIN_ID
    assert db.commits == 1

    audits = db.added_of(AuditLog)
    assert len(audits) == 1
    assert audits[0].action == "settings.patch"
    assert audits[0].target == "settings"
    assert audits[0].actor_id == ADMIN_ID
    assert audits[0].before_snapshot == {"TOP_K": 5}
    assert audits[0].after_snapshot == {"TOP_K": 9}

    assert cache.deleted == 1, "写后删缓存（Redis + 本进程）"


@pytest.mark.asyncio
async def test_a_restart_tier_write_does_not_delete_the_cache(cache: Any) -> None:
    """第二档不参与热更：删缓存不会让任何人看到新值，所以不必删（写了也没人读）。"""

    db = _session()

    result = await runtime_settings.patch_runtime_settings(
        db, values={"CHUNK_SIZE": 256}, dry_run=False, actor=_actor(), cache=cache
    )

    assert result.restart == ("CHUNK_SIZE",)
    assert result.hot == ()
    assert "重启" in result.note
    assert cache.deleted == 0


@pytest.mark.asyncio
async def test_a_write_is_visible_to_the_next_refresh_without_waiting_for_the_ttl(
    cache: Any,
) -> None:
    """写一次之后**立刻**读：新值马上生效，不必等 30 秒。

    这正是「写后删缓存」的验收形态：删的是缓存与本进程的「刚检查过」标记，于是下一次
    ``ensure`` 会重新回源，而不是继续用上一轮合并出来的值。
    """

    db = _session(_row("TOP_K", 5))
    runtime_settings.ensure_runtime_overrides(db, cache=cache)
    assert settings.TOP_K == 5

    async with async_transaction(db):
        await runtime_settings.patch_runtime_settings(
            db, values={"TOP_K": 12}, dry_run=False, actor=_actor(), cache=cache
        )
    runtime_settings.ensure_runtime_overrides(db, cache=cache)

    assert settings.TOP_K == 12


@pytest.mark.asyncio
async def test_a_secret_value_is_masked_in_the_panel_and_in_the_audit(cache: Any) -> None:
    """审计列表是管理员都能读的一页；把明文 Key 再抄一份进去只是扩大泄露面。"""

    db = _session(_row("MODEL_API_KEY", "sk-plaintext-abcd"))

    result = await runtime_settings.patch_runtime_settings(
        db,
        values={"MODEL_API_KEY": "sk-rotated-wxyz"},
        dry_run=False,
        actor=_actor(),
        cache=cache,
    )

    assert "sk-rotated-wxyz" not in str(result.after)
    assert result.after["MODEL_API_KEY"].endswith("wxyz")
    audit = db.added_of(AuditLog)[-1]
    assert "sk-plaintext-abcd" not in str(audit.before_snapshot)
    assert "sk-rotated-wxyz" not in str(audit.after_snapshot)


# ────────────────────────────────────────────────────────────────────── 影响预览


def _impact_world(*, mutual_tenants: int = 6) -> dict[type, list[Any]]:
    """``mutual_tenants`` 个「pro 套餐 + quality 档」的租户，外加一个免费档的 speed 租户。

    用六家而不是两家，是为了把「``count`` 是全量、``sample`` 只有 5 个」这件事测出来。
    """

    plans: list[Plan] = []
    for index, code in enumerate(("free", "standard", "pro"), start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        plans.append(row)

    tenants: list[Tenant] = []
    policies: list[KeyPolicy] = []
    for number in range(1, mutual_tenants + 1):
        tenant_id = f"t{number}"
        tenants.append(Tenant(id=tenant_id, name=f"租户{number}", plan_id=3, status="active"))
        policies.append(
            KeyPolicy(
                id=f"p{number}",
                tenant_id=tenant_id,
                name=f"quality-{number}",
                allowed_kb_ids=[],
                retrieve_qps=10,
                retrieve_daily=5000,
                profile="quality",
            )
        )
    tenants.append(Tenant(id="free-1", name="免费租户", plan_id=1, status="active"))
    policies.append(
        KeyPolicy(
            id="p-free",
            tenant_id="free-1",
            name="free-speed",
            allowed_kb_ids=[],
            retrieve_qps=3,
            retrieve_daily=500,
            profile="speed",
        )
    )
    return {Plan: plans, Tenant: tenants, KeyPolicy: policies}


@pytest.mark.asyncio
async def test_turning_off_rerank_reports_every_quality_tenant(cache: Any, monkeypatch) -> None:
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    db = _session(world=_impact_world(mutual_tenants=6))

    impact = await runtime_settings.preview_impact(db, {"RERANK_ENABLED": False})

    assert impact.quality_degraded.count == 6, "count 必须是全量"
    assert len(impact.quality_degraded.sample) == 5, "sample 只放前 5 个名字"
    assert impact.quality_degraded.sample[0] == "租户1"
    assert impact.keyword_channel_lost.count == 0


@pytest.mark.asyncio
async def test_turning_off_rewrite_also_degrades_quality(cache: Any, monkeypatch) -> None:
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)
    db = _session(world=_impact_world())

    impact = await runtime_settings.preview_impact(db, {"QUERY_REWRITE_ENABLED": False})

    assert impact.quality_degraded.count == 6


@pytest.mark.asyncio
async def test_turning_off_the_keyword_channel_reports_every_hybrid_tenant(
    cache: Any, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "KEYWORD_SEARCH_PROVIDER", "elasticsearch")
    db = _session(world=_impact_world())

    impact = await runtime_settings.preview_impact(db, {"KEYWORD_SEARCH_PROVIDER": "none"})

    assert impact.keyword_channel_lost.count == 6
    assert len(impact.keyword_channel_lost.sample) == 5
    assert impact.quality_degraded.count == 0


@pytest.mark.asyncio
async def test_nothing_is_reported_when_the_capability_is_already_off(cache: Any) -> None:
    """已经是关的，再关一次不改变任何租户：预览必须报 0，而不是把「配置存在」当影响。"""

    db = _session(world=_impact_world())

    impact = await runtime_settings.preview_impact(db, {"RERANK_ENABLED": False})

    assert impact.quality_degraded.count == 0
    assert impact.keyword_channel_lost.count == 0


@pytest.mark.asyncio
async def test_a_policy_whose_plan_never_granted_the_capability_is_not_reported(
    cache: Any, monkeypatch
) -> None:
    """免费档租户的配置本来就没在跑重排：关掉部署开关不会改变它的结果。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    world = _impact_world(mutual_tenants=1)
    world[KeyPolicy].append(
        KeyPolicy(
            id="p-free-quality",
            tenant_id="free-1",
            name="mislabelled",
            allowed_kb_ids=[],
            retrieve_qps=3,
            retrieve_daily=500,
            profile="quality",
        )
    )
    db = _session(world=world)

    impact = await runtime_settings.preview_impact(db, {"RERANK_ENABLED": False})

    assert impact.quality_degraded.count == 1, "免费档那条配置不该被算进影响面"
