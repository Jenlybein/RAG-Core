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

from app.core.exceptions import ErrorCode
from tests.support.policy import policy_row

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.config import settings
from app.models.account import Account
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy, policy_snapshot_of
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.tenant import Tenant
from tests.fake_db import MemoryPlanCache, RecordingSession, override_all_db
from tests.support.keys import mint_key

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

#: 世界里的两把 Key：行与原文一起造（库里只存哈希，原文只在这一处留着）。
_ALPHA_KEY = mint_key(
    key_id=KEY_ALPHA, tenant_id=TENANT_ALPHA, policy_id=POLICY_ALPHA, label="alpha-bot"
)
_BETA_KEY = mint_key(
    key_id=KEY_BETA, tenant_id=TENANT_BETA, policy_id=POLICY_BETA, label="beta-bot"
)
KEY_ALPHA_VALUE = _ALPHA_KEY.plaintext
KEY_BETA_VALUE = _BETA_KEY.plaintext
#: 面板与审计里唯一能看见的那一截（末 5 位）。
ALPHA_TAIL = _ALPHA_KEY.secret[-5:]

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: RecordingSession
    cache: MemoryPlanCache

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

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
    mode: str = "vector",
    top_k: int = 5,
    rerank: bool = False,
    allowed_kb_ids: list[str] | None = None,
    qps: int = 10,
    daily: int = 5000,
) -> KeyPolicy:
    """一套自洽的密钥配置：要重排就得同时给重排位，否则它自己就不成立。"""

    return policy_row(
        policy_id=policy_id,
        tenant_id=tenant_id,
        name=name,
        allowed_kb_ids=tuple(allowed_kb_ids or ()),
        rewrite_allowed=False,
        decompose_allowed=False,
        research_allowed=False,
        rerank_allowed=rerank,
        retrieve_qps=qps,
        retrieve_daily=daily,
        rerank_qps=0,
        rerank_daily=0,
        llm_qps=0,
        llm_daily=0,
        custom_options=tuple(sorted({"mode": mode, "top_k": top_k, "rerank": rerank}.items())),
        include_parent=True,
        expand_table_rows=True,
    )


def _api_key(key_id: str, tenant_id: str, policy_id: str, value: str, label: str) -> ApiKey:
    """世界里的行：``value`` 是原文，哈希与末 5 位由 :mod:`tests.support.keys` 算。"""

    return mint_key(
        key_id=key_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        label=label,
        plaintext=value,
    ).api_key


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
                mode="hybrid",
                top_k=5,
                rerank=True,
                allowed_kb_ids=["kb-a1"],
            ),
            _policy(POLICY_BETA, TENANT_BETA, "beta-default", mode="hybrid", top_k=5),
        ],
        ApiKey: [_ALPHA_KEY.api_key, _BETA_KEY.api_key],
    }


@pytest.fixture
def console() -> Any:
    """一个真实应用 + 假数据库 + 假缓存；每个用例一套全新的世界。"""

    import importlib

    main = importlib.import_module("app.main")
    console_deps = importlib.import_module("app.api.console_deps")

    db = RecordingSession(rows=_world())
    cache = MemoryPlanCache()
    # 读与写都在异步栈上（票 #130）：两个异步会话入口一起换、喂同一个假库，于是登录写下的
    # 会话行在下一条请求里读得到。
    override_all_db(main.app, db)
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=db, cache=cache)
    finally:
        main.app.dependency_overrides.clear()


# ──────────────────────────────────────────── 闭环：签发 → 用那一串调数据面

#: 数据面上「只看自己」的那条读接口：它过一次完整鉴权（拆串 + 哈希校验 + 租户 + 套餐）。
ME_PATH = "/api/v1/auth/me"


def test_the_issued_plaintext_authenticates_and_the_list_never_shows_it_again(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端一次：签发拿到的原文能直接调数据面；之后再怎么读也拿不回它。

    这条是「只此一次」的验收：签发响应里有原文、库里只有哈希、列表里只有末 5 位，
    而那一串真能通过鉴权——四件事必须在同一条用例里对上，否则前三条都可以是假的。
    """

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    issued = console.data(_issue(console))
    plaintext = issued["key_plaintext"]

    response = console.client.get(ME_PATH, headers={"Authorization": f"Bearer {plaintext}"})

    assert response.json()["code"] == 0, response.json()
    assert response.json()["data"]["tenant"]["tenant_id"] == TENANT_ALPHA

    # 同一个 token 第二次照样能用（走的是校验缓存），行为不因此改变。
    again = console.client.get(ME_PATH, headers={"Authorization": f"Bearer {plaintext}"})
    assert again.json()["code"] == 0

    # 列表里再也看不到它：连末 5 位之外的任何一个字符都不该出现。
    body = console.client.get("/console/keys").text
    assert plaintext not in body
    assert f"rc_live_…{plaintext[-5:]}" in body


# ─────────────────────────────────────────────────────────────── GET /console/keys


def test_listing_keys_returns_the_masked_form_and_no_plaintext(console: _Harness) -> None:
    """列表里只有打码形态：原文在签发之后就取不回来了（库里只有 scrypt 哈希）。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    body = console.client.get("/console/keys").text
    keys = console.data(console.client.get("/console/keys"))["keys"]

    assert [item["key_id"] for item in keys] == [KEY_ALPHA]
    item = keys[0]
    assert item["label"] == "alpha-bot"
    assert "key_value" not in item, "列表里不该再有这个字段：它只是一个恒为空的名字"
    assert item["key_masked"] == f"rc_live_…{ALPHA_TAIL}"
    assert KEY_ALPHA_VALUE not in body
    assert item["status"] == "active"
    policy = item["policy"]
    assert policy["policy_id"] == POLICY_ALPHA
    assert policy["retrieval_mode"] == "hybrid"
    assert policy["allowed_kb_ids"] == ["kb-a1"]
    assert policy["retrieval_mode"] == "hybrid"
    assert policy["rerank_allowed"] is True
    assert policy["rewrite_allowed"] is False
    assert policy["research_allowed"] is False
    assert (policy["retrieve_qps"], policy["retrieve_daily"]) == (10, 5000)
    assert policy["include_parent"] is True
    assert policy["expand_table_rows"] is True
    assert (policy["retrieval_top_k"], policy["retrieval_rerank"]) == (5, True)


def test_listing_keys_never_mentions_another_tenant(console: _Harness) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    body = console.client.get("/console/keys").text

    assert KEY_BETA in body
    assert KEY_BETA_VALUE not in body, "本租户的原文也不该出现在列表里"
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
    "retrieve_allowed": True,
    "rewrite_allowed": False,
    "decompose_allowed": False,
    "research_allowed": False,
    "rerank_allowed": False,
    "retrieve_qps": 5,
    "retrieve_daily": 1000,
    "rerank_qps": 0,
    "rerank_daily": 0,
    "llm_qps": 0,
    "llm_daily": 0,
    "retrieval_mode": "hybrid",
    "retrieval_top_k": 5,
    "retrieval_rerank": False,
    "include_parent": True,
    "expand_table_rows": False,
}


def _issue(console: _Harness, *, label: str = "alpha-new", **policy: Any) -> Any:
    body = {"label": label, "policy": {**_ISSUE_POLICY, **policy}}
    return console.client.post("/console/keys", json=body, headers=WRITE_HEADERS)


def test_issuing_a_key_creates_its_policy_and_returns_the_plaintext_once(
    console: _Harness,
) -> None:
    """签发是一整套：Key + 配置 + 一条审计，同一个事务里落定。

    ``key_plaintext`` 是原文**唯一**一次露面：行里只有哈希与末 5 位。
    """

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits

    response = _issue(console)
    data = console.data(response)

    assert data["key_id"] not in (KEY_ALPHA, KEY_BETA)
    assert data["label"] == "alpha-new"
    plaintext = data["key_plaintext"]
    assert plaintext.startswith(f"rc_live_{data['key_id']}."), "串里必须带行身份证"
    assert len(plaintext) == len("rc_live_") + len(data["key_id"]) + 1 + 32
    assert data["key_masked"] == f"rc_live_…{plaintext[-5:]}"
    assert "key_value" not in data
    assert data["status"] == "active"
    assert data["policy"]["retrieval_mode"] == "hybrid"
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
    assert entry.after_snapshot["policy"]["retrieval_mode"] == "hybrid"
    # 审计是跨租户给管理员看的：这里只有打码形态，且原文一个子串都不许出现。
    assert entry.after_snapshot["key_masked"] == data["key_masked"]
    assert plaintext not in str(entry.after_snapshot)
    # 库里那一行也只有哈希与末 5 位：签发响应之外再也拿不到原文。
    row = console.key_row(data["key_id"])
    assert row.key_hash.startswith("scrypt$")
    assert row.key_tail == plaintext[-5:]


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

    response = console.client.post("/console/keys", json={"label": "x", "policy": _ISSUE_POLICY})

    assert console.code(response) == 20011
    assert console.db.added_of(ApiKey) == []
    assert console.audit_entries() == []


def test_issuing_a_policy_over_the_plan_limit_is_a_20015(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = _issue(console, retrieve_qps=999)

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
    assert entry.before_snapshot["key_masked"] == f"rc_live_…{ALPHA_TAIL}"
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
    assert data["after"]["retrieval_mode"] == "hybrid"
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


def test_a_policy_patch_opens_research_and_takes_effect_without_reissuing_the_key(
    console: _Harness,
) -> None:
    """Research 的两个额度在面板上可开（票 #150），改完下一次调用就按新值判。

    「不必重签 Key」不是一句承诺：判定内核每个请求都重读这一行配置（见
    ``key_policy_service`` 模块文档），所以这里断言的是**那一行映射出来的快照**——
    正是下一次调用会拿到的输入。
    """

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_ALPHA}",
        json={"research_allowed": True, "llm_qps": 2, "llm_daily": 200},
        headers=WRITE_HEADERS,
    )

    data = console.data(response)
    assert data["after"]["research_allowed"] is True
    assert (data["after"]["llm_qps"], data["after"]["llm_daily"]) == (2, 200)
    assert {"research_allowed", "llm_qps", "llm_daily"} <= set(data["changed"])

    row = console.policy_row(POLICY_ALPHA)
    assert (row.llm_qps, row.llm_daily) == (2, 200)
    snapshot = policy_snapshot_of(row)
    assert (snapshot.llm_qps, snapshot.llm_daily) == (2, 200), (
        "下一次调用拿到的快照就是新值：不必重签 Key"
    )

    entry = console.audit_entries()[-1]
    assert entry.action == "key_policy.update"
    assert entry.before_snapshot["llm_qps"] == 0
    assert entry.after_snapshot["llm_qps"] == 2
    assert entry.after_snapshot["llm_daily"] == 200


def test_a_policy_patch_rejects_a_negative_research_quota(console: _Harness) -> None:
    """0 合法（= 关闭），负数不合法：额度不是一个可以取负的东西。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    audits_before = len(console.audit_entries())

    response = console.client.patch(
        f"/console/key-policies/{POLICY_ALPHA}",
        json={"llm_daily": -1},
        headers=WRITE_HEADERS,
    )

    assert console.code(response) == 20015
    assert console.policy_row(POLICY_ALPHA).llm_daily == 0
    assert len(console.audit_entries()) == audits_before


def test_a_policy_patch_over_the_plan_limit_is_a_20015(console: _Harness) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}", json={"retrieve_qps": 11}, headers=WRITE_HEADERS
    )

    assert console.code(response) == 20015
    assert console.policy_row(POLICY_BETA).retrieve_qps == 10


def test_a_policy_patch_beyond_the_plan_quota_is_a_20015(
    console: _Harness,
) -> None:
    assert console.login("bob", USER_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        f"/console/key-policies/{POLICY_BETA}",
        json={"retrieve_daily": 10**9},
        headers=WRITE_HEADERS,
    )

    assert console.code(response) == 20015
    assert console.policy_row(POLICY_BETA).retrieve_daily != 10**9


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


# ────────────────────────────────────── 三项直参数（第 13 步：档位退役）

_NEW_PARAMS = {"retrieval_mode": "bm25", "retrieval_top_k": 7, "retrieval_rerank": False}


def _patch_policy(console: _Harness, policy_id: str, payload: dict[str, Any]) -> Any:
    return console.client.patch(
        f"/console/key-policies/{policy_id}", json=payload, headers=WRITE_HEADERS
    )


def test_the_three_parameters_are_writeable_and_read_back(console: _Harness) -> None:
    """三项参数各自是一列，改完立刻回显——不存在「写进 JSON 但没人读」的中间态。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    data = console.data(_patch_policy(console, POLICY_ALPHA, dict(_NEW_PARAMS)))

    assert data["after"]["retrieval_mode"] == "bm25"
    assert data["after"]["retrieval_top_k"] == 7
    assert data["after"]["retrieval_rerank"] is False
    assert set(data["changed"]) == set(_NEW_PARAMS)
    row = console.policy_row(POLICY_ALPHA)
    assert (row.retrieval_mode, row.retrieval_top_k, row.retrieval_rerank) == ("bm25", 7, False)


def test_an_unknown_mode_is_a_20015_not_a_request_shape_error(console: _Harness) -> None:
    """拼错一个模式是**配置不成立**（20015），不是请求结构错误（20002）。

    两者的补救动作不同：20002 要改调用，20015 要改配置内容。
    """

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = _patch_policy(console, POLICY_ALPHA, {"retrieval_mode": "bing"})

    assert console.code(response) == ErrorCode.PARAM_ERROR
    assert console.policy_row(POLICY_ALPHA).retrieval_mode != "bing"


def test_a_top_k_below_one_is_a_20015(console: _Harness) -> None:
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    response = _patch_policy(console, POLICY_ALPHA, {"retrieval_top_k": 0})

    assert console.code(response) == ErrorCode.PARAM_ERROR
    assert console.policy_row(POLICY_ALPHA).retrieval_top_k != 0


def test_the_parameters_are_patched_one_at_a_time_without_touching_the_others(
    console: _Harness,
) -> None:
    """面板一次只改一个输入框：只发一个字段，另外两个保持原样。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0
    before = console.policy_row(POLICY_ALPHA)

    _patch_policy(console, POLICY_ALPHA, {"retrieval_mode": "bm25"})
    _patch_policy(console, POLICY_ALPHA, {"retrieval_top_k": 9})

    row = console.policy_row(POLICY_ALPHA)
    assert row.retrieval_mode == "bm25"
    assert row.retrieval_top_k == 9
    assert row.retrieval_rerank == before.retrieval_rerank
