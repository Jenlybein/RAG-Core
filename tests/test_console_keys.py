"""票 #91：控制面 Key 与密钥配置（``/console/keys``、``/console/key-policies``）的边界验收。

接缝与 ``test_console_api.py`` 同一套：真实应用 + ``TestClient``，只把数据库换成
``RecordingSession``、套餐缓存换成内存假实现。业务函数一律不 monkeypatch——这样测到的
是「租户隔离、自洽校验、删 Key 不删配置、每次改动留账」这些**行为**，而不是某个函数有
没有被调用。

世界刻意让两个租户落在不同套餐上：Alpha 是 ``pro``（允许 ``quality``），Beta 是
``standard``（白名单只有 ``speed`` / ``balanced`` / ``custom``）。「档位越出套餐白名单」
因此可以真的撞上一次，而不是靠改测试数据造出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.models.account import Account
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from tests.fake_db import RecordingSession

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

POLICY_ALPHA = "policy-alpha"
POLICY_BETA = "policy-beta"

KEY_ALPHA = "key-alpha"
KEY_BETA = "key-beta"

#: 与 ``new_api_key_value()`` 同形：``rc_live_`` + 32 位十六进制；末四位拿来验 masked。
KEY_ALPHA_VALUE = "rc_live_" + "0123456789abcdef0123456789ab9f2c"
KEY_BETA_VALUE = "rc_live_" + "fedcba9876543210fedcba9876543210"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


class _MemoryPlanCache:
    """内存假缓存：``get/set/delete`` 三个方法就是全部接缝。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, value: Any) -> None:
        self.items[plan_id] = value

    def delete(self, plan_id: int) -> None:
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

    def code(self, response: Any) -> int:
        return int(response.json()["code"])

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]

    def rows(self, model: type) -> list[Any]:
        return list(self.db._rows.get(model, []))

    def key_row(self, key_id: str) -> ApiKey:
        return next(row for row in self.rows(ApiKey) if row.id == key_id)

    def policy_row(self, policy_id: str) -> KeyPolicy:
        return next(row for row in self.rows(KeyPolicy) if row.id == policy_id)


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


def _policy(
    policy_id: str,
    tenant_id: str,
    name: str,
    *,
    profile: str,
    allowed_kb_ids: list[str] | None = None,
    hybrid: bool = False,
    rerank: bool = False,
    rewrite: bool = False,
    qps: int = 10,
    daily: int = 5000,
) -> KeyPolicy:
    """一套自洽的密钥配置：``quality`` 必须把三件事都打开，否则它自己就不成立。"""

    return KeyPolicy(
        id=policy_id,
        tenant_id=tenant_id,
        name=name,
        allowed_kb_ids=list(allowed_kb_ids or []),
        research_allowed=False,
        hybrid_allowed=hybrid,
        rerank_allowed=rerank,
        query_rewrite_allowed=rewrite,
        retrieve_qps=qps,
        retrieve_daily=daily,
        profile=profile,
        include_parent=True,
        expand_table_rows=True,
        custom_options={},
    )


def _api_key(key_id: str, tenant_id: str, policy_id: str, value: str, label: str) -> ApiKey:
    return ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_value=value,
        label=label,
        status="active",
    )


def _world() -> dict[type, list[Any]]:
    return {
        Plan: _plan_rows(),
        Tenant: [
            Tenant(id=TENANT_ALPHA, name="Alpha Corp", plan_id=3, status="active"),
            Tenant(id=TENANT_BETA, name="Beta Corp", plan_id=2, status="active"),
        ],
        Account: [
            _account(ADMIN_ID, "admin", ADMIN_PASSWORD, role="admin"),
            _account(ALICE_ID, "alice", USER_PASSWORD, tenant_id=TENANT_ALPHA),
            _account(BOB_ID, "bob", USER_PASSWORD, tenant_id=TENANT_BETA),
        ],
        KeyPolicy: [
            _policy(
                POLICY_ALPHA,
                TENANT_ALPHA,
                "alpha-default",
                profile="quality",
                allowed_kb_ids=["kb-a1"],
                hybrid=True,
                rerank=True,
                rewrite=True,
            ),
            _policy(POLICY_BETA, TENANT_BETA, "beta-default", profile="speed", hybrid=True),
        ],
        ApiKey: [
            _api_key(KEY_ALPHA, TENANT_ALPHA, POLICY_ALPHA, KEY_ALPHA_VALUE, "alpha-bot"),
            _api_key(KEY_BETA, TENANT_BETA, POLICY_BETA, KEY_BETA_VALUE, "beta-bot"),
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


# ─────────────────────────────────────────────────────────────── GET /console/keys


def test_listing_keys_returns_the_plaintext_and_the_masked_form(console: _Harness) -> None:
    """ADR-0004 的明确取舍：调试台要拿原文打真实接口，所以本票返回明文。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    keys = console.data(console.client.get("/console/keys"))["keys"]

    assert [item["key_id"] for item in keys] == [KEY_ALPHA]
    item = keys[0]
    assert item["label"] == "alpha-bot"
    assert item["key_value"] == KEY_ALPHA_VALUE
    assert item["key_masked"] == "rc_live_…9f2c"
    assert item["status"] == "active"
    policy = item["policy"]
    assert policy["policy_id"] == POLICY_ALPHA
    assert policy["profile"] == "quality"
    assert policy["allowed_kb_ids"] == ["kb-a1"]
    assert policy["hybrid_allowed"] is True
    assert policy["rerank_allowed"] is True
    assert policy["query_rewrite_allowed"] is True
    assert policy["research_allowed"] is False
    assert (policy["retrieve_qps"], policy["retrieve_daily"]) == (10, 5000)
    assert policy["include_parent"] is True
    assert policy["expand_table_rows"] is True
    assert policy["custom_options"] == {}


def test_listing_keys_never_mentions_another_tenant(console: _Harness) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    body = console.client.get("/console/keys").text

    assert KEY_BETA in body
    assert KEY_BETA_VALUE in body
    assert KEY_ALPHA not in body
    assert POLICY_ALPHA not in body


def test_listing_keys_as_an_admin_without_a_tenant_is_forbidden(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.get("/console/keys")

    assert console.code(response) == 20011


def test_listing_keys_without_a_session_is_unauthorized(console: _Harness) -> None:
    response = console.client.get("/console/keys")

    assert console.code(response) == 20010


# ──────────────────────────────────────────────────────────── POST /console/keys

#: 一套自洽的默认签发请求：``pro`` 套餐下 ``balanced`` 只需要 hybrid。
_ISSUE_POLICY = {
    "allowed_kb_ids": ["kb-a1", "kb-a2"],
    "research_allowed": False,
    "hybrid_allowed": True,
    "rerank_allowed": False,
    "query_rewrite_allowed": False,
    "retrieve_qps": 5,
    "retrieve_daily": 1000,
    "profile": "balanced",
    "include_parent": True,
    "expand_table_rows": False,
    "custom_options": {},
}


def _issue(console: _Harness, *, label: str = "alpha-new", **policy: Any) -> Any:
    body = {"label": label, "policy": {**_ISSUE_POLICY, **policy}}
    return console.client.post("/console/keys", json=body, headers=WRITE_HEADERS)


def test_issuing_a_key_creates_its_policy_and_returns_the_plaintext(console: _Harness) -> None:
    """签发是一整套：Key + 配置 + 一条审计，同一个事务里落定。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits

    response = _issue(console)
    data = console.data(response)

    assert data["key_id"] not in (KEY_ALPHA, KEY_BETA)
    assert data["label"] == "alpha-new"
    assert data["key_value"].startswith("rc_live_")
    assert data["key_masked"] == f"rc_live_…{data['key_value'][-4:]}"
    assert data["status"] == "active"
    assert data["policy"]["profile"] == "balanced"
    assert data["policy"]["allowed_kb_ids"] == ["kb-a1", "kb-a2"]
    assert data["policy"]["expand_table_rows"] is False

    # 两行一起写：租户是调用方自己的，配置是这次签发显著要求新建的那一套。
    new_keys = console.db.added_of(ApiKey)
    new_policies = console.db.added_of(KeyPolicy)
    assert [row.id for row in new_keys] == [data["key_id"]]
    assert [row.id for row in new_policies] == [data["policy"]["policy_id"]]
    assert new_keys[0].tenant_id == TENANT_ALPHA
    assert new_keys[0].policy_id == new_policies[0].id
    assert new_policies[0].tenant_id == TENANT_ALPHA
    assert new_policies[0].retrieve_qps == 5
    assert console.db.commits == commits_before + 1

    entry = console.audit_entries()[-1]
    assert entry.action == "key.issue"
    assert entry.target == f"key:{data['key_id']}"
    assert entry.before_snapshot is None
    assert entry.after_snapshot["policy"]["profile"] == "balanced"
    # 审计是跨租户给管理员看的：这里只留 masked，不留可用凭证。
    assert entry.after_snapshot["key_masked"] == data["key_masked"]
    assert data["key_value"] not in str(entry.after_snapshot)


def test_a_key_issue_stays_in_the_callers_own_tenant(console: _Harness) -> None:
    """租户不是请求参数：签发出来的两行都落在调用方自己的租户上。"""

    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    console.data(_issue(console, label="beta-new"))

    assert console.db.added_of(ApiKey)[0].tenant_id == TENANT_BETA
    assert console.db.added_of(KeyPolicy)[0].tenant_id == TENANT_BETA


def test_issuing_a_key_as_an_admin_without_a_tenant_is_forbidden(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = _issue(console)

    assert console.code(response) == 20011
    assert console.db.added_of(ApiKey) == []


def test_issuing_a_key_without_a_session_is_unauthorized(console: _Harness) -> None:
    response = _issue(console)

    assert console.code(response) == 20010


def test_issuing_a_key_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.post(
        "/console/keys", json={"label": "x", "policy": _ISSUE_POLICY}
    )

    assert console.code(response) == 20011
    assert console.db.added_of(ApiKey) == []
    assert console.audit_entries() == []


def test_issuing_a_profile_whose_capabilities_are_off_is_a_20015(console: _Harness) -> None:
    """``quality`` 承诺重排 + 改写；配置开关没开，这套配置落地那一刻就自相矛盾。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits

    response = _issue(console, profile="quality", rerank_allowed=False, query_rewrite_allowed=False)

    assert console.code(response) == 20015
    assert console.db.added_of(ApiKey) == []
    assert console.db.added_of(KeyPolicy) == []
    assert console.db.commits == commits_before
    assert console.audit_entries() == []


def test_issuing_a_policy_over_the_plan_limit_is_a_20015(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = _issue(console, retrieve_qps=999)

    assert console.code(response) == 20015
    assert console.db.added_of(ApiKey) == []
    assert console.db.added_of(KeyPolicy) == []


def test_issuing_an_unknown_profile_is_a_20015(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = _issue(console, profile="turbo")

    assert console.code(response) == 20015
    assert console.db.added_of(ApiKey) == []
    assert console.db.added_of(KeyPolicy) == []


# ──────────────────────────────────────────────────────── DELETE /console/keys/{id}


def test_deleting_a_key_keeps_its_policy_row(console: _Harness) -> None:
    """配置独立于 Key：结构上配置能被多把 Key 复用，删 Key 是删凭证，不是删配置。"""

    from app.repositories import key_policy_repository

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.delete(f"/console/keys/{KEY_ALPHA}", headers=WRITE_HEADERS)

    data = console.data(response)
    assert data == {"key_id": KEY_ALPHA, "deleted": True}
    assert console.db.get(ApiKey, KEY_ALPHA) is None
    # 用 repository 直接断言「配置行还在」——绕过 HTTP 响应，测的是存储事实。
    assert key_policy_repository.get_by_id(console.db, POLICY_ALPHA) is not None
    assert console.db.added_of(KeyPolicy) == []

    entry = console.audit_entries()[-1]
    assert entry.action == "key.delete"
    assert entry.target == f"key:{KEY_ALPHA}"
    assert entry.before_snapshot["key_masked"] == "rc_live_…9f2c"
    assert entry.after_snapshot is None

    # 删完列表里就只剩其它 Key（这里本租户本来只有这一把，于是空）。
    assert console.data(console.client.get("/console/keys"))["keys"] == []


def test_deleting_another_tenants_key_is_not_found(console: _Harness) -> None:
    """跨租户按「不存在」处理：不泄露「这个 id 确实有一把 Key」这一位信息。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.delete(f"/console/keys/{KEY_BETA}", headers=WRITE_HEADERS)

    assert console.code(response) == 20016
    assert console.db.get(ApiKey, KEY_BETA) is not None
    assert console.audit_entries() == []


def test_deleting_an_unknown_key_is_not_found(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.delete("/console/keys/does-not-exist", headers=WRITE_HEADERS)

    assert console.code(response) == 20016


def test_deleting_a_key_as_an_admin_without_a_tenant_is_forbidden(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.delete(f"/console/keys/{KEY_ALPHA}", headers=WRITE_HEADERS)

    assert console.code(response) == 20011
    assert console.db.get(ApiKey, KEY_ALPHA) is not None


def test_deleting_a_key_without_a_session_is_unauthorized(console: _Harness) -> None:
    response = console.client.delete(f"/console/keys/{KEY_ALPHA}", headers=WRITE_HEADERS)

    assert console.code(response) == 20010


def test_deleting_a_key_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.delete(f"/console/keys/{KEY_ALPHA}")

    assert console.code(response) == 20011
    assert console.db.get(ApiKey, KEY_ALPHA) is not None
    assert console.audit_entries() == []


# ────────────────────────────────────────────── PATCH /console/key-policies/{id}


def test_a_policy_patch_is_a_partial_update_with_a_before_and_after_snapshot(
    console: _Harness,
) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits

    response = console.client.patch(
        f"/console/key-policies/{POLICY_ALPHA}",
        json={"retrieve_qps": 20, "retrieve_daily": 2000, "expand_table_rows": False},
        headers=WRITE_HEADERS,
    )

    data = console.data(response)
    assert data["policy_id"] == POLICY_ALPHA
    assert data["before"]["retrieve_qps"] == 10
    assert data["after"]["retrieve_qps"] == 20
    assert data["after"]["retrieve_daily"] == 2000
    assert data["after"]["expand_table_rows"] is False
    assert data["changed"] == ["expand_table_rows", "retrieve_daily", "retrieve_qps"]
    # 没给的字段保持原样：档位还是 quality、白名单还是那一个库。
    assert data["after"]["profile"] == "quality"
    assert data["after"]["allowed_kb_ids"] == ["kb-a1"]

    row = console.policy_row(POLICY_ALPHA)
    assert (row.retrieve_qps, row.retrieve_daily) == (20, 2000)
    assert row.expand_table_rows is False
    assert console.db.commits == commits_before + 1

    entry = console.audit_entries()[-1]
    assert entry.action == "key_policy.update"
    assert entry.target == f"policy:{POLICY_ALPHA}"
    assert entry.before_snapshot["retrieve_qps"] == 10
    assert entry.after_snapshot["retrieve_qps"] == 20


def test_a_policy_patch_can_move_to_a_profile_the_plan_allows(console: _Harness) -> None:
    """``standard`` 允许 ``balanced``：改档位是正常动作，不必新签 Key。"""

    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}", json={"profile": "balanced"}, headers=WRITE_HEADERS
    )

    data = console.data(response)
    assert data["after"]["profile"] == "balanced"
    assert console.policy_row(POLICY_BETA).profile == "balanced"


def test_a_policy_patch_to_a_profile_outside_the_plan_is_a_20015(console: _Harness) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}", json={"profile": "quality"}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20015
    assert console.policy_row(POLICY_BETA).profile == "speed"
    assert console.db.commits == commits_before
    assert console.audit_entries() == []


def test_a_policy_patch_over_the_plan_limit_is_a_20015(console: _Harness) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}", json={"retrieve_qps": 11}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20015
    assert console.policy_row(POLICY_BETA).retrieve_qps == 10


def test_a_policy_patch_that_grants_a_capability_the_plan_lacks_is_a_20015(
    console: _Harness,
) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}",
        json={"rerank_allowed": True},
        headers=WRITE_HEADERS,
    )

    assert console.code(response) == 20015
    assert console.policy_row(POLICY_BETA).rerank_allowed is False


def test_a_policy_patch_on_another_tenants_policy_is_not_found(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}", json={"retrieve_qps": 20}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20016
    assert console.policy_row(POLICY_BETA).retrieve_qps == 10
    assert console.audit_entries() == []


def test_a_policy_patch_on_an_unknown_policy_is_not_found(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/key-policies/does-not-exist", json={"retrieve_qps": 20}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20016


def test_a_policy_patch_as_an_admin_without_a_tenant_is_forbidden(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_ALPHA}", json={"retrieve_qps": 20}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20011
    assert console.policy_row(POLICY_ALPHA).retrieve_qps == 10


def test_a_policy_patch_without_a_session_is_unauthorized(console: _Harness) -> None:
    response = console.client.patch(
        f"/console/key-policies/{POLICY_ALPHA}", json={"retrieve_qps": 20}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20010


def test_a_policy_patch_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_ALPHA}", json={"retrieve_qps": 20}
    )

    assert console.code(response) == 20011
    assert console.policy_row(POLICY_ALPHA).retrieve_qps == 10
    assert console.audit_entries() == []



