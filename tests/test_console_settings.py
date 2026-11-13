"""票 #93：配置中台的 ASGI 边界验收（``GET/PATCH /console/settings`` 与表的形状）。

接缝是**真实应用 + ``TestClient``**：只看 HTTP 响应体与数据库里真正落了什么。存储换成
``RecordingSession``、缓存换成内存假实现——不连真库、不连 Redis。

**权限口径是这一组用例的主线**：配置中台是**平台级**配置（不属于任何租户），一次修改
横跨所有租户，所以它只给管理员——管理员能读能写；**租户账号读/写都必须是 ``20011``**
（这正是要钉住的跨租户越权边）；未登录 ``20010``；写请求缺 ``x-requested-with`` →
``20011``。业务判定的细节（合并四步、逐键校验、影响预览口径）在
``tests/test_runtime_settings.py``。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, ForeignKeyConstraint, inspect
from sqlalchemy.dialects.postgresql import JSONB

import app.models as models_package
from app.core import passwords
from app.core.config import settings
from app.core.runtime_settings_cache import reset_settings_cache, set_settings_cache
from app.db.base import Base
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.runtime_setting import RuntimeSetting
from app.models.tenant import Tenant
from app.services import runtime_settings
from tests.fake_db import MemoryPlanCache, RecordingSession, override_all_db

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}

SEEDED_AT = datetime(2026, 1, 1, 8, 0, 0)

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


class _MemorySettingsCache:
    """内存假缓存：三个方法就是全部接缝，另带一份「删过几次」的账本。"""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.deleted: int = 0

    def get(self) -> dict[str, Any] | None:
        return None if self.payload is None else dict(self.payload)

    def set(self, overrides: dict[str, Any]) -> None:
        self.payload = dict(overrides)

    def delete(self) -> None:
        self.deleted += 1
        self.payload = None

    async def adelete(self) -> None:
        """循环上的那条：与 ``delete`` 同一份状态（契约见 app.core.runtime_settings_cache）。"""

        self.delete()


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: RecordingSession
    cache: _MemorySettingsCache

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

    def data(self, response: Any) -> Any:
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]

    def add_setting(self, key: str, value: Any, *, updated_at: datetime | None = None) -> None:
        """预置一条覆盖行（模拟「有人绕过接口写库」或「上一次保存的结果」）。"""

        self.db._rows.setdefault(RuntimeSetting, []).append(
            RuntimeSetting(
                key=key,
                value=value,
                updated_by=ADMIN_ID,
                updated_at=updated_at or SEEDED_AT,
            )
        )

    def setting_row(self, key: str) -> RuntimeSetting:
        return next(row for row in self.db._rows[RuntimeSetting] if row.key == key)

    def settings(self, response: Any) -> dict[str, dict[str, Any]]:
        """把三组铺平成「键 → 行」，断言就不必每次都写一遍分组遍历。"""

        rows = [row for group in self.data(response)["groups"] for row in group["settings"]]
        return {row["key"]: row for row in rows}

    def add_policy(self, policy: KeyPolicy) -> None:
        self.db._rows.setdefault(KeyPolicy, []).append(policy)


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _account(
    account_id: str,
    username: str,
    password: str,
    *,
    role: str = "user",
    tenant_id: str | None = None,
) -> Account:
    return Account(
        id=account_id,
        tenant_id=tenant_id,
        username=username,
        password_hash=passwords.hash_password(password),
        role=role,
        status="active",
    )


def _policy(policy_id: str, tenant_id: str, profile: str) -> KeyPolicy:
    return KeyPolicy(
        id=policy_id,
        tenant_id=tenant_id,
        name=policy_id,
        allowed_kb_ids=[],
        retrieve_qps=10,
        retrieve_daily=5000,
        profile=profile,
    )


def _world() -> dict[type, list[Any]]:
    """最小世界：平台管理员（无租户）、两个租户账号、三个套餐、两条 quality 配置。

    两个租户都挂 ``quality`` 档，是为了让「关掉重排」的影响面数出 2 个租户来；配置中台
    本身是平台级的，与调用它的是哪个租户账号无关——这正是「跨租户不适用」的对照形态。
    """

    return {
        Plan: _plan_rows(),
        Tenant: [
            Tenant(id=TENANT_ALPHA, name="Alpha Corp", plan_id=3, status="active"),
            Tenant(id=TENANT_BETA, name="Beta Corp", plan_id=3, status="active"),
        ],
        Account: [
            _account(ADMIN_ID, "admin", ADMIN_PASSWORD, role="admin"),
            _account(ALICE_ID, "alice", USER_PASSWORD, tenant_id=TENANT_ALPHA),
            _account(BOB_ID, "bob", USER_PASSWORD, tenant_id=TENANT_BETA),
        ],
        KeyPolicy: [
            _policy("policy-alpha", TENANT_ALPHA, "quality"),
            _policy("policy-beta", TENANT_BETA, "quality"),
        ],
        #: 开局**一行覆盖都没有**：`.env` 是缺省，这条回归红线在端点这一层也要成立。
        RuntimeSetting: [],
    }


@pytest.fixture
def console() -> Any:
    """真实应用 + 假数据库 + 假缓存 + 假会话工厂；每个用例一套全新的世界与状态。"""

    import importlib

    main = importlib.import_module("app.main")
    console_deps = importlib.import_module("app.api.console_deps")

    saved = settings.model_dump()
    runtime_settings.reset_runtime_state()
    db = RecordingSession(rows=_world())
    cache = _MemorySettingsCache()
    plan_cache = MemoryPlanCache()
    set_settings_cache(cache)
    # 中间件自己开会话（它拿不到 Depends）；注入同一个假会话，于是刷新路径也在测试里。
    runtime_settings.set_session_factory(lambda: db)
    # 配置清单的 GET 与 PATCH 都在异步栈上（票 #129 / #130）：两个异步会话入口一起换。
    override_all_db(main.app, db)
    main.app.dependency_overrides[console_deps.get_runtime_settings_cache] = lambda: cache
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: plan_cache
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=db, cache=cache)
    finally:
        main.app.dependency_overrides.clear()
        runtime_settings.reset_runtime_state()
        runtime_settings.set_session_factory(None)
        reset_settings_cache()
        for key, value in saved.items():
            setattr(settings, key, value)


# ──────────────────────────────────────────────────────────────────── 鉴权四例


def test_a_settings_call_without_a_session_is_unauthorized(console: _Harness) -> None:
    assert console.client.get("/console/settings").json()["code"] == 20010
    assert (
        console.client.patch("/console/settings", json={"values": {"TOP_K": 9}}).json()["code"]
        == 20010
    )


def test_a_tenant_account_cannot_read_or_write_platform_settings(console: _Harness) -> None:
    """**跨租户越权边**：租户账号读/写配置中台一律 ``20011``。

    这条比「管理员没有租户」重要得多：``runtime_settings`` 是平台级配置，``RERANK_ENABLED``
    一关就是**所有租户**的 quality 档位一起退化。让一个租户账号能 PATCH 它，等于给每个
    租户一把改别人服务质量的钥匙。所以两个租户账号（Alpha / Beta）都必须被挡在 20011，
    而且被挡的写操作不许留下任何库行。
    """

    for username in ("alice", "bob"):
        assert console.login(username, USER_PASSWORD).json()["code"] == 0

        assert console.client.get("/console/settings").json()["code"] == 20011, username
        assert (
            console.client.patch(
                "/console/settings?dry_run=false",
                json={"values": {"TOP_K": 9}},
                headers=WRITE_HEADERS,
            ).json()["code"]
            == 20011
        ), username

    assert console.db.added_of(RuntimeSetting) == []


def test_an_admin_without_a_tenant_can_read_and_write_platform_settings(
    console: _Harness,
) -> None:
    """管理员 ``tenant_id is None`` **正是**这份配置唯一合理的归属：平台级配置没有租户。

    所以这里不叠加任何租户判定——叠加上去会把唯一合法的写者挡在 20011 门外。管理员能读、
    能写，写出来的审计 actor 是它自己的账号 id。
    """

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    rows = console.settings(console.client.get("/console/settings"))
    assert rows["TOP_K"]["tier"] == "hot"

    response = console.client.patch(
        "/console/settings?dry_run=false",
        json={"values": {"TOP_K": 9}},
        headers=WRITE_HEADERS,
    )
    assert response.json()["code"] == 0
    assert console.setting_row("TOP_K").updated_by == ADMIN_ID
    assert console.audit_entries()[-1].actor_id == ADMIN_ID


def test_a_patch_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits  # 登录本身会提交一条会话行

    response = console.client.patch(
        "/console/settings?dry_run=false", json={"values": {"TOP_K": 9}}
    )

    assert response.json()["code"] == 20011
    assert console.db.added_of(RuntimeSetting) == []
    assert console.db.commits == commits_before


# ────────────────────────────────────────────────────────────── 列表：三档与来源标记


def test_the_listing_returns_three_tiers_with_editable_and_why_columns(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    data = console.data(console.client.get("/console/settings"))

    assert data["cache_ttl_seconds"] == 30, "面板上「各进程 30s 内生效」的依据"
    assert [group["tier"] for group in data["groups"]] == ["locked", "restart", "hot"]
    assert [group["editable"] for group in data["groups"]] == [False, True, True]
    for group in data["groups"]:
        assert group["title"], "每一档都要有一句中文小标题"
        assert group["settings"], "空组说明白名单漏了这一档"

    rows = console.settings(console.client.get("/console/settings"))
    for key in ("AUTH_ENABLED", "RATE_LIMIT_ENABLED", "DEV_API_KEY", "DEV_TENANT_ID"):
        assert rows[key]["tier"] == "locked"
        assert rows[key]["editable"] is False
        assert rows[key]["why"], f"{key} 必须写明「为什么不能改」"
    assert rows["LLM_MODEL"]["tier"] == "hot"
    assert rows["LLM_MODEL"]["editable"] is True
    assert rows["CHUNK_SIZE"]["tier"] == "restart"
    assert rows["RERANK_TIMEOUT_SECONDS"]["source"] == "env"
    assert rows["RERANK_TIMEOUT_SECONDS"]["invalid"] is False
    assert rows["model.role.fast"]["tier"] == "hot"


def test_an_illegal_row_marks_the_line_red_and_falls_back_to_the_env_value(
    console: _Harness,
) -> None:
    """有人绕过接口把库里写成 ``-1``：读时回退 + 标红，列表照常返回、服务不崩。"""

    console.add_setting("RERANK_TIMEOUT_SECONDS", -1)
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    row = console.settings(console.client.get("/console/settings"))["RERANK_TIMEOUT_SECONDS"]

    assert row["invalid"] is True
    assert row["invalid_reason"] and "≥" in row["invalid_reason"]
    assert row["source"] == "env"
    assert row["value"] == runtime_settings.ENV_BASELINE["RERANK_TIMEOUT_SECONDS"]
    # 一行坏掉不影响别的行，进程也照常服务。
    assert settings.RERANK_TIMEOUT_SECONDS == row["value"]
    assert console.client.get("/console/me").json()["code"] == 0


def test_a_valid_db_override_is_reported_with_source_db(console: _Harness) -> None:
    console.add_setting("TOP_K", 8)
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    row = console.settings(console.client.get("/console/settings"))["TOP_K"]

    assert (row["source"], row["invalid"], row["value"]) == ("db", False, 8)


def test_a_secret_override_is_masked_in_the_listing(console: _Harness) -> None:
    """面板与审计里都不许出现明文 Key；这是 #82「密钥类字段必须 masked」的那一条。"""

    console.add_setting("MODEL_API_KEY", "sk-plaintext-abcd")
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.get("/console/settings")

    assert "sk-plaintext-abcd" not in response.text
    assert response.json()["data"] is not None


# ────────────────────────────────────────────────────────────── 写入：dry_run 与校验


def test_dry_run_is_the_default_and_nothing_is_written(console: _Harness) -> None:
    """忘了 ``?dry_run=`` 时只试算：最坏结果是「少改了一次」，不是「改了生产配置」。"""

    console.add_setting("TOP_K", 5)
    updated_before = console.setting_row("TOP_K").updated_at
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/settings", json={"values": {"TOP_K": 9}}, headers=WRITE_HEADERS
    )

    data = console.data(response)
    assert data["dry_run"] is True
    assert data["updated"] == []
    # 不数提交次数：写接口在请求边界上开一笔事务，dry_run 时它提交的是**空的一笔**。
    # 「什么都没写」的证据是下面两条——没有行被 add、updated_at 没动。
    assert console.db.added_of(RuntimeSetting) == []
    assert console.setting_row("TOP_K").updated_at == updated_before, "dry_run 不许改 updated_at"
    assert data["impact"] is not None


def test_a_key_outside_the_whitelist_is_a_param_error(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/settings?dry_run=false",
        json={"values": {"NOT_A_SETTING": 1}},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == 20015
    assert console.db.added_of(RuntimeSetting) == []


def test_a_locked_security_switch_is_a_param_error(console: _Harness) -> None:
    """``AUTH_ENABLED`` 在白名单里（面板要展示），但**写入**一律 ``20015``。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/settings?dry_run=false",
        json={"values": {"AUTH_ENABLED": False}},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == 20015
    assert console.db.added_of(RuntimeSetting) == []


def test_an_illegal_value_is_a_param_error(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    for values in ({"TOP_K": "abc"}, {"RERANK_TIMEOUT_SECONDS": -1}, {"RERANK_ENABLED": "false"}):
        response = console.client.patch(
            "/console/settings?dry_run=false", json={"values": values}, headers=WRITE_HEADERS
        )
        assert response.json()["code"] == 20015, values


def test_a_real_patch_writes_audits_and_deletes_the_cache(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits  # 登录本身会提交一条会话行

    response = console.client.patch(
        "/console/settings?dry_run=false",
        json={"values": {"TOP_K": 9}},
        headers=WRITE_HEADERS,
    )
    data = console.data(response)

    assert data["dry_run"] is False
    assert data["updated"] == ["TOP_K"]
    assert data["hot"] == ["TOP_K"]
    assert "30s" in data["note"], "面板要如实写「各进程 30s 内生效」"
    assert console.db.commits == commits_before + 1
    assert console.cache.deleted == 1

    row = console.setting_row("TOP_K")
    assert row.value == 9
    assert row.updated_by == ADMIN_ID

    audit = console.audit_entries()[-1]
    assert (audit.action, audit.target, audit.actor_id) == ("settings.patch", "settings", ADMIN_ID)
    assert audit.before_snapshot == {"TOP_K": None}
    assert audit.after_snapshot == {"TOP_K": 9}


def test_a_hot_patch_takes_effect_on_the_next_request_without_the_ttl(
    console: _Harness,
) -> None:
    """验收原话：改掉一项热生效配置后，**下一次请求**就按新值跑（不用重启、不等 30 秒）。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    assert settings.TOP_K == runtime_settings.ENV_BASELINE["TOP_K"]

    console.client.patch(
        "/console/settings?dry_run=false",
        json={"values": {"TOP_K": 9}},
        headers=WRITE_HEADERS,
    )
    # 下一个请求（中间件会刷新）应当已经按新值跑。
    console.client.get("/console/settings")

    assert settings.TOP_K == 9


def test_a_restart_tier_patch_reports_pending_restart_and_keeps_the_cache(
    console: _Harness,
) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/settings?dry_run=false",
        json={"values": {"CHUNK_SIZE": 256}},
        headers=WRITE_HEADERS,
    )

    data = console.data(response)
    assert data["restart"] == ["CHUNK_SIZE"]
    assert data["hot"] == []
    assert "重启" in data["note"]
    assert console.cache.deleted == 0, "第二档不热更：删缓存没有意义"
    assert console.setting_row("CHUNK_SIZE").value == 256


def test_dry_run_previews_the_impact_of_turning_off_rerank(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改能力开关前必须走影响预览：列出哪些租户的 quality 会退化。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/settings", json={"values": {"RERANK_ENABLED": False}}, headers=WRITE_HEADERS
    )

    impact = console.data(response)["impact"]
    assert impact["quality_degraded"] == {"count": 2, "sample": ["Alpha Corp", "Beta Corp"]}
    assert impact["keyword_channel_lost"] == {"count": 0, "sample": []}
    assert console.db.added_of(RuntimeSetting) == []


def test_dry_run_previews_the_keyword_channel_loss(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "KEYWORD_SEARCH_PROVIDER", "elasticsearch")
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    impact = console.data(
        console.client.patch(
            "/console/settings",
            json={"values": {"KEYWORD_SEARCH_PROVIDER": "none"}},
            headers=WRITE_HEADERS,
        )
    )["impact"]

    assert impact["keyword_channel_lost"] == {"count": 2, "sample": ["Alpha Corp", "Beta Corp"]}


# ───────────────────────────────────────────────────────────────── 表与迁移的形状


def test_the_runtime_settings_table_is_registered_and_keyed_by_the_setting_name() -> None:
    assert "runtime_settings" in Base.metadata.tables
    assert "RuntimeSetting" in models_package.__all__

    table = RuntimeSetting.__table__
    assert [column.name for column in table.primary_key] == ["key"]
    assert table.c.key.type.length == 128
    assert isinstance(table.c.value.type, JSONB)
    assert isinstance(table.c.updated_at.type, DateTime)
    assert table.c.updated_by.type.length == 64
    assert not table.c.updated_by.nullable


def test_the_runtime_settings_table_carries_no_foreign_keys_and_no_relationships() -> None:
    """不加外键：``updated_by`` 指向的账号可以被删，引用完整性靠应用层校验。"""

    table = RuntimeSetting.__table__
    assert not table.foreign_keys
    assert not [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(inspect(RuntimeSetting).relationships) == 0


class _RecordingOp:
    """假 ``op``：只记录建了什么、删了什么；``execute`` 一律失败（迁移不许灌数据）。"""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.dropped: list[str] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, table_name: str, *_args: Any, **_kwargs: Any) -> None:
        self.created.append(f"table:{table_name}")

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        self.dropped.append(f"table:{table_name}")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移只建一张表，不该用到 op.{name}")


def _load_migration() -> tuple[ModuleType, _RecordingOp]:
    path = next(VERSIONS_DIR.glob("*_create_runtime_settings.py"))
    spec = importlib.util.spec_from_file_location("t93_runtime_settings_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    recorder = _RecordingOp()
    module.op = recorder
    return module, recorder


def test_the_migration_creates_exactly_the_runtime_settings_table_and_drops_it_again() -> None:
    module, recorder = _load_migration()

    module.upgrade()
    created = sorted(recorder.created)
    module.downgrade()

    assert created == ["table:runtime_settings"]
    assert sorted(recorder.dropped) == created


def test_the_migration_chains_onto_the_previous_head() -> None:
    path = next(VERSIONS_DIR.glob("*_create_runtime_settings.py"))
    text = path.read_text(encoding="utf-8")
    revision = re.search(r"""^revision(?::\s*str)?\s*=\s*['"]([^'"]+)['"]""", text, re.M)
    down = re.search(r"""^down_revision(?::\s*[^=]+)?\s*=\s*['"]([^'"]+)['"]""", text, re.M)

    assert revision is not None and re.fullmatch(r"[0-9a-f]{12}", revision.group(1))
    assert down is not None and down.group(1) == "d4b7e2c5a890"
    assert path.name == f"{revision.group(1)}_create_runtime_settings.py"
