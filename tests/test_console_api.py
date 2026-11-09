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
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.config import settings
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of, plan_spec_of
from app.models.tenant import Tenant
from tests.fake_db import RecordingSession

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


class _MemoryPlanCache:
    """内存假缓存：``get/set/delete`` 三个方法就是全部接缝。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}
        self.set_calls: list[int] = []
        self.deleted: list[int] = []

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, plan: Any) -> None:
        self.set_calls.append(plan_id)
        self.items[plan_id] = plan

    def delete(self, plan_id: int) -> None:
        self.deleted.append(plan_id)
        self.items.pop(plan_id, None)


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: RecordingSession
    cache: _MemoryPlanCache

    def login(self, username: str, password: str) -> Any:
        return self.client.post(
            "/console/login", json={"username": username, "password": password}
        )

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
                profile="quality",
            ),
            KeyPolicy(
                id="policy-beta",
                tenant_id=TENANT_BETA,
                name="beta-default",
                allowed_kb_ids=[],
                retrieve_qps=10,
                retrieve_daily=5000,
                profile="speed",
            ),
        ],
    }


@pytest.fixture
def console() -> Any:
    """一个真实应用 + 假数据库 + 假缓存；每个用例一套全新的世界。"""

    import importlib

    main = importlib.import_module("app.main")
    session_module = importlib.import_module("app.db.session")
    console_deps = importlib.import_module("app.api.console_deps")

    db = RecordingSession(rows=_world())
    cache = _MemoryPlanCache()
    main.app.dependency_overrides[session_module.get_db] = lambda: db
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=db, cache=cache)
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

    response = console.client.patch(
        "/console/plans/3", json={"max_kb": 2}, headers=WRITE_HEADERS
    )
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
    commits_before = console.db.commits
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
    assert console.db.commits == commits_before
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

    fresh = plan_service.resolve_plan(console.db, 3, cache=console.cache)
    assert fresh.max_kb == 2
    with pytest.raises(AppError) as raised:
        static_quota.ensure_kb_quota(console.db, tenant_id=TENANT_ALPHA, plan=fresh)
    assert raised.value.code == ErrorCode.QUOTA_EXCEEDED


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
