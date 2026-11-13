"""票 #80：控制面最低闭环的 ASGI 边界验收。

接缝只有一条：真实应用 + ``TestClient``，只看 HTTP 响应体、响应头与 cookie 行为。
数据库换成本仓既有的 ``RecordingSession``（它现在也能回答 ``SELECT count(*)``，因为
改套餐影响面要数库数），套餐缓存换成内存假实现——**不连真库、不连 Redis**。

这里刻意不 monkeypatch 业务函数：登录、鉴权、试算、落库、删缓存、写审计全部走真代码，
只有「存储」与「缓存」两个外部依赖被替换。这样测到的才是「面板接口的每一次查询都按
租户过滤、每一次改动都留账」这些**行为**，而不是某个函数有没有被调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncio
import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core import redis_client
from app.core.config import settings
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of, plan_spec_of
from app.models.tenant import Tenant
from app.services.quota_gate import QuotaCounters, daily_key
from tests.fake_db import MemoryPlanCache, RecordingSession, override_all_db

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: RecordingSession
    cache: MemoryPlanCache
    db_view: Any = None

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

    def data(self, response: Any) -> Any:
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]

    def plan_row(self, plan_id: int) -> Plan:
        return next(row for row in self.db._rows[Plan] if row.id == plan_id)

    @staticmethod
    def session_id_of(response: Any) -> str:
        raw = response.headers["set-cookie"]
        head = raw.split(";", 1)[0]
        _name, value = head.split("=", 1)
        return value

    def use_cookie(self, session_id: str) -> None:
        """把浏览器里的会话换回指定的旧 cookie（同一个 TestClient 只存一份）。"""

        self.client.cookies.set("console_session", session_id, path="/console")


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


def _kb(kb_id: str, tenant_id: str) -> KnowledgeBase:
    return KnowledgeBase(
        id=kb_id,
        tenant_id=tenant_id,
        name=kb_id,
        description=None,
        split_strategy="structure",
        allowed_formats=["markdown"],
        settings={"chunk_size": 512},
    )


def _world() -> dict[type, list[Any]]:
    """一套最小但完整的世界：两种角色、两个租户、三档套餐、四个库、两套密钥配置。

    影响面测试要用到的形状都在这里：Alpha 有 3 个库（多于新的 ``max_kb=2``），且它的
    密钥配置挂着 ``quality`` 档（新的白名单只留 ``speed``）；Beta 两个条件都不撞。
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
        KnowledgeBase: [
            _kb("kb-a1", TENANT_ALPHA),
            _kb("kb-a2", TENANT_ALPHA),
            _kb("kb-a3", TENANT_ALPHA),
            _kb("kb-b1", TENANT_BETA),
        ],
        KeyPolicy: [
            KeyPolicy(
                id="policy-alpha",
                tenant_id=TENANT_ALPHA,
                name="alpha-default",
                allowed_kb_ids=[],
                retrieve_qps=10,
                retrieve_daily=5000,
                research_qps=0,
                research_daily=0,
                profile="quality",
            ),
            KeyPolicy(
                id="policy-beta",
                tenant_id=TENANT_BETA,
                name="beta-default",
                allowed_kb_ids=[],
                retrieve_qps=10,
                retrieve_daily=5000,
                research_qps=0,
                research_daily=0,
                profile="speed",
            ),
        ],
    }


@pytest.fixture
def console() -> Any:
    """一个真实应用 + 假数据库 + 假缓存；每个用例一套全新的世界。"""

    import importlib

    main = importlib.import_module("app.main")
    console_deps = importlib.import_module("app.api.console_deps")

    db = RecordingSession(rows=_world())
    cache = MemoryPlanCache()
    # 读与写都在异步栈上（票 #130）：两个异步会话入口一起换，喂**同一个**假库，于是登录写下
    # 的会话行在下一个请求里读得到（只换同步那条会让身份判定真的去连库）。
    view = override_all_db(main.app, db)
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=db, cache=cache, db_view=view)
    finally:
        main.app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────── 未登录 / 无权限


def test_console_call_without_a_session_is_unauthorized(console: _Harness) -> None:
    response = console.client.get("/console/me")

    assert response.json()["code"] == 20010


def test_data_plane_key_cannot_open_the_console(console: _Harness) -> None:
    """数据面的凭证不认：面板读的是 cookie，``Authorization`` 在这里等于没带。"""

    response = console.client.get(
        "/console/me", headers={"authorization": f"Bearer {settings.DEV_API_KEY}"}
    )

    assert response.json()["code"] == 20010


def test_the_console_dependency_is_a_different_code_path(console: _Harness) -> None:
    from app.api.console_deps import get_console_account
    from app.api.deps import get_current_key

    assert get_console_account is not get_current_key
    assert get_console_account.__module__ != get_current_key.__module__


def test_a_non_admin_gets_forbidden_on_every_admin_endpoint(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    for method, path in (
        ("GET", "/console/plans"),
        ("GET", "/console/accounts"),
        ("GET", "/console/audit-logs"),
    ):
        response = console.client.request(method, path)
        assert response.json()["code"] == 20011, path

    response = console.client.patch("/console/plans/3", json={"max_kb": 2}, headers=WRITE_HEADERS)
    assert response.json()["code"] == 20011

    response = console.client.patch(
        f"/console/accounts/{BOB_ID}", json={"status": "disabled"}, headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 20011


# ───────────────────────────────────────────────────────────────── 登录 / 登出


def test_login_sets_an_httponly_lax_cookie_and_returns_my_view(console: _Harness) -> None:
    response = console.login("alice", USER_PASSWORD)

    assert response.json()["code"] == 0
    cookie = response.headers["set-cookie"].lower().replace(" ", "")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/console" in cookie
    data = response.json()["data"]
    assert data["account"]["username"] == "alice"
    assert data["tenant"]["tenant_id"] == TENANT_ALPHA
    assert data["plan"]["code"] == "pro"


def test_production_cookies_are_secure(console: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "production")

    response = console.login("alice", USER_PASSWORD)

    assert response.json()["code"] == 0
    assert "secure" in response.headers["set-cookie"].lower()


def test_local_cookies_are_not_secure_because_http_would_drop_them(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "APP_ENV", "local")

    response = console.login("alice", USER_PASSWORD)

    assert "secure" not in response.headers["set-cookie"].lower()


def test_a_wrong_password_is_indistinguishable_from_an_unknown_username(console: _Harness) -> None:
    wrong = console.login("alice", "not-the-password")
    unknown = console.login("nobody", "not-the-password")

    assert wrong.json()["code"] == 20010
    assert unknown.json()["code"] == 20010
    assert wrong.json()["msg"] == unknown.json()["msg"]


def test_logout_removes_the_server_row_and_the_browser_cookie(console: _Harness) -> None:
    response = console.login("alice", USER_PASSWORD)
    session_id = console.session_id_of(response)
    assert console.client.get("/console/me").json()["code"] == 0

    response = console.client.post("/console/logout", headers=WRITE_HEADERS)

    assert response.json()["code"] == 0
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert [row.id for row in console.db.deleted] == [session_id]
    assert console.client.cookies.get("console_session") is None
    assert console.client.get("/console/me").json()["code"] == 20010


def test_logout_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.post("/console/logout")

    assert response.json()["code"] == 20011
    # 被拒的登出不该产生副作用：cookie 还在，会话行也还在。
    assert console.client.get("/console/me").json()["code"] == 0


# ─────────────────────────────────────────────────────── 停用 / 换套餐（管理员）


def test_disabling_an_account_makes_its_old_cookie_stop_working_immediately(
    console: _Harness,
) -> None:
    """不需要等会话过期：每个请求都回账号表现读状态。"""

    alice = console.login("alice", USER_PASSWORD)
    alice_session = console.session_id_of(alice)
    assert console.client.get("/console/me").json()["code"] == 0

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    response = console.client.patch(
        f"/console/accounts/{ALICE_ID}", json={"status": "disabled"}, headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 0

    console.use_cookie(alice_session)
    assert console.client.get("/console/me").json()["code"] == 20010


def test_an_admin_can_disable_a_tenant_and_the_audit_keeps_both_sides(
    console: _Harness,
) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/accounts/{ALICE_ID}",
        json={"tenant_status": "disabled"},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == 0
    assert next(row for row in console.db._rows[Tenant] if row.id == TENANT_ALPHA).status == (
        "disabled"
    )
    entry = console.audit_entries()[-1]
    assert entry.actor_id == ADMIN_ID
    assert entry.action == "account.update"
    assert entry.before_snapshot["tenant_status"] == "active"
    assert entry.after_snapshot["tenant_status"] == "disabled"


def test_changing_a_tenants_plan_is_visible_to_the_user_on_the_next_request(
    console: _Harness,
) -> None:
    alice_session = console.session_id_of(console.login("alice", USER_PASSWORD))

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    response = console.client.patch(
        f"/console/accounts/{ALICE_ID}", json={"plan_id": 1}, headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 0

    tenant = next(row for row in console.db._rows[Tenant] if row.id == TENANT_ALPHA)
    assert tenant.plan_id == 1

    console.use_cookie(alice_session)
    me = console.client.get("/console/me")
    assert me.json()["code"] == 0
    assert me.json()["data"]["plan"]["code"] == "free"


def test_an_admin_sees_no_tenant_or_plan_of_its_own(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    me = console.client.get("/console/me")

    assert me.json()["code"] == 0
    assert me.json()["data"]["tenant"] is None
    assert me.json()["data"]["plan"] is None


def test_an_admin_lists_every_plan_and_every_account(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    plans = console.client.get("/console/plans").json()["data"]["plans"]
    assert [plan["code"] for plan in plans] == ["free", "standard", "pro"]

    accounts = console.client.get("/console/accounts").json()["data"]["accounts"]
    assert {item["username"] for item in accounts} == {"admin", "alice", "bob"}
    alice = next(item for item in accounts if item["username"] == "alice")
    assert alice["tenant_name"] == "Alpha Corp"
    assert alice["plan"]["code"] == "pro"
    admin = next(item for item in accounts if item["username"] == "admin")
    assert admin["tenant_id"] is None
    assert admin["plan"] is None


def test_my_view_never_mentions_another_tenant(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    body = console.client.get("/console/me").text

    assert TENANT_ALPHA in body
    assert TENANT_BETA not in body
    assert "bob" not in body


# ─────────────────────────────────────────────────────────── 改套餐：试算与落库

_IMPACT_PATCH = {"max_kb": 2, "allowed_profiles": ["speed"]}


def test_dry_run_returns_the_impact_but_writes_nothing(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    console.cache.items[3] = plan_spec_of(console.plan_row(3))
    added_before = len(console.db.added)
    audits_before = len(console.audit_entries())

    response = console.client.patch(
        "/console/plans/3?dry_run=true", json=_IMPACT_PATCH, headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 0
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["before"]["max_kb"] == 50
    assert data["after"]["max_kb"] == 2
    assert data["impact"] == {
        "tenants_checked": 2,
        "over_max_kb": {"count": 1, "sample": ["Alpha Corp"]},
        "lose_profile": {"count": 1, "sample": ["Alpha Corp"]},
        "policies_losing_profile": 1,
    }
    assert console.plan_row(3).max_kb == 50
    # 「试算不写」断言的是**没有写行**，不是「没有提交」：一笔请求总有一笔事务，
    # 什么都没改时它提交的是空事务（见 docs/架构.md「写路径的事务边界」）。
    assert len(console.db.added) == added_before, "试算不许 add 任何行"
    assert console.db.deleted == [], "试算不许删任何行"
    assert len(console.audit_entries()) == audits_before
    assert console.cache.deleted == []


def test_dry_run_is_the_default_when_the_parameter_is_omitted(console: _Harness) -> None:
    """忘了 ``?dry_run=`` 时只试算：最坏结果是「少改了一次」，不是「改了生产配置」。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch("/console/plans/3", json=_IMPACT_PATCH, headers=WRITE_HEADERS)

    assert response.json()["code"] == 0
    assert response.json()["data"]["dry_run"] is True
    assert response.json()["data"]["impact"]["over_max_kb"]["count"] == 1
    assert console.plan_row(3).max_kb == 50


def test_a_real_patch_persists_invalidates_the_cache_and_writes_an_audit(
    console: _Harness,
) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    console.cache.items[3] = plan_spec_of(console.plan_row(3))
    commits_before = console.db.commits

    response = console.client.patch(
        "/console/plans/3?dry_run=false", json=_IMPACT_PATCH, headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 0
    data = response.json()["data"]
    assert data["dry_run"] is False
    assert data["impact"] is None
    assert data["after"]["max_kb"] == 2

    row = console.plan_row(3)
    assert row.max_kb == 2
    assert row.allowed_profiles == ["speed"]
    assert console.db.commits == commits_before + 1

    # 删缓存，不是写缓存：旧条目消失，且没有一次 set。
    assert console.cache.deleted == [3]
    assert 3 not in console.cache.items
    assert console.cache.set_calls == []

    entry = console.audit_entries()[-1]
    assert entry.actor_id == ADMIN_ID
    assert entry.action == "plan.update"
    assert entry.target == "plan:3"
    assert entry.before_snapshot["max_kb"] == 50
    assert entry.after_snapshot["max_kb"] == 2
    assert entry.created_at is not None

    listed = console.client.get("/console/audit-logs").json()["data"]["audit_logs"]
    assert listed[0]["target"] == "plan:3"
    assert listed[0]["before"]["max_kb"] == 50
    assert listed[0]["after"]["max_kb"] == 2

    # 「改完立刻生效」的落地形态：下一次读套餐走的是库里的新值，而不是缓存里的旧值；
    # 于是下一个建库请求按新上限判，Alpha 的 3 个库已经超了 `max_kb=2`。
    from app.core.exceptions import AppError, ErrorCode
    from app.services import plan_service, static_quota

    # 套餐解析自票 #129 起是协程（缓存换成了 redis.asyncio），这里用一次 ``asyncio.run``
    # 在同步用例里取它的结果：断言的是「库里是新值」，与「谁在等」无关。会话取自
    # ``override_all_db`` 造的那个异步视图——套餐行与库数读的正是它。
    async def _after_patch() -> None:
        fresh = await plan_service.resolve_plan(console.db_view, 3, cache=console.cache)
        assert fresh.max_kb == 2
        with pytest.raises(AppError) as raised:
            await static_quota.ensure_kb_quota(console.db_view, tenant_id=TENANT_ALPHA, plan=fresh)
        assert raised.value.code == ErrorCode.QUOTA_EXCEEDED

    asyncio.run(_after_patch())


def test_an_unknown_profile_is_a_plan_invalid_error(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/plans/3?dry_run=false",
        json={"allowed_profiles": ["qualty"]},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == 20018
    assert console.plan_row(3).allowed_profiles != ["qualty"]
    assert console.audit_entries() == []


def test_a_non_positive_limit_is_a_plan_invalid_error(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/plans/3?dry_run=false", json={"max_kb": 0}, headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 20018
    assert console.plan_row(3).max_kb == 50
    assert console.audit_entries() == []


def test_a_referenced_plan_cannot_be_deleted(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/plans/3?dry_run=false", json={"operation": "delete"}, headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 20019
    assert any(row.id == 3 for row in console.db._rows[Plan])


def test_an_unreferenced_plan_can_be_deleted_and_the_audit_has_only_a_before_side(
    console: _Harness,
) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/plans/1?dry_run=false", json={"operation": "delete"}, headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 0
    data = response.json()["data"]
    assert data["deleted"] is True
    assert data["before"]["code"] == "free"
    assert data["after"] is None
    assert not any(row.id == 1 for row in console.db._rows[Plan])
    entry = console.audit_entries()[-1]
    assert entry.action == "plan.delete"
    assert entry.before_snapshot["code"] == "free"
    assert entry.after_snapshot is None


def test_a_plan_patch_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch("/console/plans/3?dry_run=false", json={"max_kb": 2})

    assert response.json()["code"] == 20011
    assert console.plan_row(3).max_kb == 50
    assert console.cache.deleted == []


def test_an_unknown_plan_is_plan_not_found(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.get("/console/plans")
    assert response.json()["code"] == 0

    response = console.client.patch(
        "/console/plans/99?dry_run=false", json={"max_kb": 2}, headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 20017


def test_an_unknown_account_is_not_found(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/accounts/does-not-exist",
        json={"status": "disabled"},
        headers=WRITE_HEADERS,
    )

    assert response.json()["code"] == 20016


# ───────────────────────────────────────────── 今日用量：只读、可空、不编 0（Gap 2）


class _CounterStore:
    """与配额服务同形的内存计数器：``incr/get/expire/delete`` 四条命令 + 调用账本。

    读取路径只能留下 ``get:``；``calls`` 让「面板没有在任何地方偷偷 +1」可以被直接断言。
    命令是**异步**的（票 #133）：注入缝换成了 ``redis.asyncio`` 的客户端。
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[str] = []

    async def incr(self, key: str) -> int:
        self.calls.append(f"incr:{key}")
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str) -> str | None:
        self.calls.append(f"get:{key}")
        value = self.values.get(key)
        return None if value is None else str(value)

    async def expire(self, key: str, ttl: int) -> None:
        self.calls.append(f"expire:{key}")
        self.ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self.calls.append(f"delete:{key}")
        self.values.pop(key, None)
        self.ttls.pop(key, None)


class _ExplodingCounterStore:
    """任何命令都炸：开关关着时，登录与 ``/me`` 都不该碰 Redis 一次。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def incr(self, key: str) -> int:  # pragma: no cover - 访问即失败
        self.calls.append(f"incr:{key}")
        raise AssertionError(f"不该访问 Redis：incr({key})")

    async def get(self, key: str) -> str | None:  # pragma: no cover - 访问即失败
        self.calls.append(f"get:{key}")
        raise AssertionError(f"不该访问 Redis：get({key})")

    async def expire(self, key: str, ttl: int) -> None:  # pragma: no cover - 访问即失败
        self.calls.append(f"expire:{key}")
        raise AssertionError(f"不该访问 Redis：expire({key}, {ttl})")

    async def delete(self, key: str) -> None:  # pragma: no cover - 访问即失败
        self.calls.append(f"delete:{key}")
        raise AssertionError(f"不该访问 Redis：delete({key})")


@pytest.fixture
def counter_store() -> Any:
    """把配额计数器的唯一注入点换成一个可观察的内存实现，用完复位。"""

    store = _CounterStore()
    redis_client.set_redis_client(store)
    try:
        yield store
    finally:
        redis_client.reset_redis_client()


def _usage_of(response: Any) -> Any:
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]["usage"]


def test_me_reports_todays_tenant_usage_when_the_counter_exists(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """开关打开、计数存在时，``/me`` 如实报出今天租户级已用次数与套餐上限。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    window = QuotaCounters(counter_store).window()
    key = daily_key(level="tenant", ident=TENANT_ALPHA, window=window)
    counter_store.values[key] = 42

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    usage = _usage_of(console.client.get("/console/me"))
    assert usage == {
        "used": 42,
        "limit": 100000,
        "source": "daily_counter",
        "note": None,
    }


def test_switch_on_without_a_counter_is_a_known_zero_not_unknown(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """开关打开时没有键 = 今天确实还没有一次成功检索，这是**已知的 0**。

    与「开关关着、平台根本没数」必须可区分：前者的 0 是事实，后者报 0 是编造。
    """

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    usage = _usage_of(console.client.get("/console/me"))
    assert usage == {
        "used": 0,
        "limit": 100000,
        "source": "daily_counter",
        "note": None,
    }


def test_me_reports_usage_as_not_tracked_when_the_switch_is_off(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关着：计数从未被写过，此时报 ``0`` 是撒谎——如实说「没有在统计」。"""

    exploding = _ExplodingCounterStore()
    redis_client.set_redis_client(exploding)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    try:
        assert console.login("alice", USER_PASSWORD).json()["code"] == 0
        usage = _usage_of(console.client.get("/console/me"))
    finally:
        redis_client.reset_redis_client()

    assert usage == {
        "used": None,
        "limit": 100000,
        "source": "not_tracked",
        "note": (
            "today's usage is not tracked: rate limiting is disabled (RATE_LIMIT_ENABLED=false)"
        ),
    }
    assert exploding.calls == [], "关掉时连一条 Redis 命令都不该发"


def test_reading_today_usage_never_increments_anything(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """读接口只读：连读两次，键的值与原样不变，且只出现 ``get``。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    window = QuotaCounters(counter_store).window()
    key = daily_key(level="tenant", ident=TENANT_ALPHA, window=window)
    counter_store.values[key] = 7

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    before = dict(counter_store.values)
    counter_store.calls.clear()

    for _ in range(2):
        assert _usage_of(console.client.get("/console/me"))["used"] == 7

    assert counter_store.values == before, "读路径不许改动任何计数"
    assert counter_store.calls, "读路径确实读了计数器"
    assert all(call.startswith("get:") for call in counter_store.calls), (
        f"读路径只允许 GET，实际：{counter_store.calls}"
    )


def test_an_admin_without_a_tenant_has_no_usage(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    usage = _usage_of(console.client.get("/console/me"))

    assert usage is None


# ───────────────────────────────────────────── 登录限流：20005、可关、成功即清（Gap 3）

#: ``TestClient`` 的默认对端地址；登录限流键里的 IP 分量就是它（不读 X-Forwarded-For）。
TESTCLIENT_IP = "testclient"


def _login_bucket_keys(username: str) -> list[str]:
    """当前与下一个五分钟桶的键。

    预置两个是为了避免「测试恰好跨过桶边界」的偶发失败：请求落在哪个桶都已经是上限。
    """

    from datetime import datetime, timezone

    from app.services.login_throttle import LOGIN_WINDOW_SECONDS, login_key

    bucket = int(datetime.now(timezone.utc).timestamp()) // LOGIN_WINDOW_SECONDS
    return [
        login_key(username=username, client_ip=TESTCLIENT_IP, window=bucket + offset)
        for offset in (0, 1)
    ]


def _login_counter_keys(counter_store: _CounterStore, username: str) -> list[str]:
    prefix = f"login:attempts:{username}:{TESTCLIENT_IP}:"
    return [key for key in counter_store.values if key.startswith(prefix)]


def test_login_is_rate_limited_with_20005_after_the_limit(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """到上限后**正确的口令也登不进来**：限流在验口令之前，暴力猜测没有免费尝试。"""

    from app.services.login_throttle import LOGIN_MAX_ATTEMPTS, MSG_LOGIN_RATE_LIMITED

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    for key in _login_bucket_keys("alice"):
        counter_store.values[key] = LOGIN_MAX_ATTEMPTS

    response = console.login("alice", USER_PASSWORD)

    body = response.json()
    assert body["code"] == 20005
    assert body["msg"] == MSG_LOGIN_RATE_LIMITED
    assert "set-cookie" not in response.headers, "被限流的登录不该下发会话 cookie"


def test_login_limit_is_a_no_op_when_the_switch_is_off(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关着时登录既不限流、也一条 Redis 命令都不发（默认套件因此离线）。"""

    exploding = _ExplodingCounterStore()
    redis_client.set_redis_client(exploding)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    try:
        wrong = console.login("alice", "not-the-password")
        right = console.login("alice", USER_PASSWORD)
    finally:
        redis_client.reset_redis_client()

    assert wrong.json()["code"] == 20010, "口令错仍是未授权，不是限流"
    assert right.json()["code"] == 0
    assert exploding.calls == [], "关掉时连一条 Redis 命令都不该发"


def test_a_failed_login_counts_the_attempt(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """失败的尝试要真的记上，否则限流永远不会触发。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    assert console.login("alice", "not-the-password").json()["code"] == 20010

    keys = _login_counter_keys(counter_store, "alice")
    assert len(keys) == 1
    assert counter_store.values[keys[0]] == 1


def test_attempts_for_an_unknown_username_are_counted_too(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """不存在的账号也要计数：否则「查无此人」就是免费的无限重试入口。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    assert console.login("ghost-user", "whatever").json()["code"] == 20010

    keys = _login_counter_keys(counter_store, "ghost-user")
    assert len(keys) == 1
    assert counter_store.values[keys[0]] == 1


def test_a_successful_login_clears_the_pairs_counter(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """输错两次后登进来：这一对的计数被清掉，而不是留到下一个窗口继续惩罚。"""

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    counter_store.values[_login_bucket_keys("alice")[0]] = 2

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    assert _login_counter_keys(counter_store, "alice") == []


def test_a_rate_limited_login_does_not_reveal_whether_the_username_exists(
    console: _Harness, monkeypatch: pytest.MonkeyPatch, counter_store: _CounterStore
) -> None:
    """已存在的账号与不存在的账号被限流时，码与消息逐字相同。"""

    from app.services.login_throttle import LOGIN_MAX_ATTEMPTS, MSG_LOGIN_RATE_LIMITED

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    for username in ("alice", "ghost-user"):
        for key in _login_bucket_keys(username):
            counter_store.values[key] = LOGIN_MAX_ATTEMPTS

    known = console.login("alice", "whatever").json()
    unknown = console.login("ghost-user", "whatever").json()

    assert known["code"] == unknown["code"] == 20005
    assert known["msg"] == unknown["msg"] == MSG_LOGIN_RATE_LIMITED
    assert "alice" not in known["msg"]
    assert "ghost-user" not in known["msg"]


# ─────────────────────────────────── 审计列表：limit / offset / action / total（#82 的分页）

#: 5 条审计，**按创建时间倒序**给出（与仓储层 ``order_by(created_at.desc())`` 的结果同序；
#: 假会话不模拟排序，所以这里直接按「查询会返回的顺序」摆好，两种实现对得上）。
_AUDIT_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("audit-5", "model.reveal", "model:cfg-a"),
    ("audit-4", "model.create", "model:cfg-b"),
    ("audit-3", "key.issue", "key:k1"),
    ("audit-2", "model.reveal", "model:cfg-a"),
    ("audit-1", "plan.update", "plan:3"),
)


def _audit_rows() -> list[AuditLog]:
    stamp = datetime(2026, 1, 5, 8, 0, 0)
    return [
        AuditLog(
            id=audit_id,
            actor_id=ADMIN_ID,
            action=action,
            target=target,
            before_snapshot=None,
            after_snapshot=None,
            created_at=stamp - timedelta(minutes=index),
        )
        for index, (audit_id, action, target) in enumerate(_AUDIT_SEEDS)
    ]


def _seed_audits(console: _Harness) -> None:
    console.db._rows[AuditLog] = _audit_rows()
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0


def test_the_audit_list_paginates_by_limit_and_offset(console: _Harness) -> None:
    """``limit=2&offset=2`` 取到列表里的第 3、4 条，``total`` 仍是被筛选的全量。"""

    _seed_audits(console)

    body = console.client.get("/console/audit-logs?limit=2&offset=2").json()
    data = body["data"]

    assert body["code"] == 0
    assert [item["audit_id"] for item in data["audit_logs"]] == ["audit-3", "audit-2"]
    assert (data["total"], data["offset"]) == (5, 2)

    first_page = console.client.get("/console/audit-logs?limit=2").json()["data"]
    assert [item["audit_id"] for item in first_page["audit_logs"]] == ["audit-5", "audit-4"]
    assert (first_page["total"], first_page["offset"]) == (5, 0)


def test_the_audit_total_is_the_whole_filtered_set_not_the_page(console: _Harness) -> None:
    """``total`` 是全量（5 条）而不是本页条数：前端靠它才能算出「总共几页」。"""

    _seed_audits(console)

    data = console.client.get("/console/audit-logs?limit=1&offset=4").json()["data"]

    assert [item["audit_id"] for item in data["audit_logs"]] == ["audit-1"]
    assert data["total"] == 5


def test_the_audit_action_filter_is_exact_match(console: _Harness) -> None:
    """``action=`` 是**精确匹配**：``model.reveal`` 不会把 ``model.create`` 带进来。"""

    _seed_audits(console)

    filtered = console.client.get("/console/audit-logs?action=model.reveal").json()["data"]
    assert [item["audit_id"] for item in filtered["audit_logs"]] == ["audit-5", "audit-2"]
    assert (filtered["total"], filtered["offset"]) == (2, 0)

    # 前缀本身不是动作名：精确匹配下它就是「没有这个动作」，而不是「筛一族」。
    prefix = console.client.get("/console/audit-logs?action=model").json()["data"]
    assert (prefix["audit_logs"], prefix["total"]) == ([], 0)


def test_a_negative_audit_offset_is_a_parameter_error(console: _Harness) -> None:
    """``offset`` 是分页起点（``ge=0``）：负数不是「从末尾倒数」，是参数非法（20002）。"""

    _seed_audits(console)

    assert console.client.get("/console/audit-logs?offset=-1").json()["code"] == 20002
