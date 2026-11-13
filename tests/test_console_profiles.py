"""档位预设的验收（``profile_presets`` 表、两条控制面端点、注册表热更新）。

接缝只有两个，其余全是真实实现：

1. **HTTP 边界**（``TestClient``）：只看响应体与库里真正落了什么；
2. **注册表**（``app.core.profile_presets``）：它在检索链路里是唯一读取点，所以
   「后台改完，下一次检索按新参数跑」这件事只有在这里能观察到——挡在它前面的每一层
   都只会拿到它的产物。

数据库换成仓库既有的 ``RecordingSession``、装载器换成空操作：**不连真库**。
（``app.core.profile_presets.set_profile_preset_loader`` 就是为此存在的注入点，
与 ``set_role_resolver`` 同一套做法。）

**这一组要钉住的越权边界**：``profile_presets`` 是平台级配置，改一行所有租户的检索质量
都跟着变，所以租户账号读/写都必须是 ``20011``；未登录 ``20010``；写请求缺
``x-requested-with`` 也 ``20011``。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.config import settings
from app.core.profile_presets import (
    DEFAULT_PRESETS,
    ProfilePresetSpec,
    get_profile_preset_loader,
    get_profile_presets,
    reset_profile_presets,
    set_profile_preset_loader,
)
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy, KeyPolicySnapshot
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.profile_preset import ProfilePreset
from app.models.tenant import Tenant
from app.services.profile_preset_service import ACTION_PROFILE_UPDATE
from app.services.retrieval_options import SEED_PROFILE_CHAINS, profile_chain_of
from tests.fake_db import RecordingSession, override_all_db

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}

SEEDED_AT = datetime(2026, 1, 1, 8, 0, 0)


def _seeded_rows() -> list[ProfilePreset]:
    """与迁移播种、与内置种子三处一致的四行（有用例钉住这一点）。"""

    rows: list[ProfilePreset] = []
    for profile, spec in DEFAULT_PRESETS.items():
        rows.append(
            ProfilePreset(
                profile=profile,
                mode=spec.mode,
                top_k=spec.top_k,
                rerank=spec.rerank,
                rewrite=spec.rewrite,
                updated_at=SEEDED_AT,
            )
        )
    return rows


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


def _world(*, presets: list[ProfilePreset] | None = None) -> dict[type, list[Any]]:
    return {
        Plan: _plan_rows(),
        Tenant: [Tenant(id=TENANT_ALPHA, name="Alpha Corp", plan_id=3, status="active")],
        Account: [
            _account(ADMIN_ID, "admin", ADMIN_PASSWORD, role="admin"),
            _account(ALICE_ID, "alice", USER_PASSWORD, tenant_id=TENANT_ALPHA),
        ],
        KeyPolicy: [],
        ProfilePreset: _seeded_rows() if presets is None else presets,
    }


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: RecordingSession

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

    def data(self, response: Any) -> Any:
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def rows(self) -> dict[str, ProfilePreset]:
        return {row.profile: row for row in self.db._rows.get(ProfilePreset, [])}

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]


@pytest.fixture
def console(request: Any) -> Any:
    """真实应用 + 假数据库 + 空装载器。每个用例一套全新的世界与注册表。"""

    main = importlib.import_module("app.main")

    presets = getattr(request, "param", None)
    db = RecordingSession(rows=_world(presets=presets))

    # 装载器换空操作：应用启动时不该去连 PostgreSQL（离线用例没有库）。同时说明
    # 「lifespan 不连库也能起来」这件事本身是被测行为——迁移没跑的库要能启动。
    saved_loader = get_profile_preset_loader()
    set_profile_preset_loader(None)
    reset_profile_presets()

    # 档位清单的 GET 与 PATCH 都在异步栈上（票 #129 / #130）：两个异步会话入口一起换。
    override_all_db(main.app, db)
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=db)
    finally:
        main.app.dependency_overrides.clear()
        reset_profile_presets()
        set_profile_preset_loader(saved_loader)


# ────────────────────────────────────────────────────────────── 鉴权四例


def test_a_call_without_a_session_is_unauthorized(console: _Harness) -> None:
    assert console.client.get("/console/profile-presets").json()["code"] == 20010
    assert (
        console.client.patch(
            "/console/profile-presets/speed", json={"top_k": 8}, headers=WRITE_HEADERS
        ).json()["code"]
        == 20010
    )


def test_a_tenant_account_cannot_read_or_write_platform_profiles(console: _Harness) -> None:
    """跨租户越权边：档位是平台级配置，租户账号读/写一律 ``20011``。"""

    console.login("alice", USER_PASSWORD)
    assert console.client.get("/console/profile-presets").json()["code"] == 20011
    assert (
        console.client.patch(
            "/console/profile-presets/quality", json={"rerank": False}, headers=WRITE_HEADERS
        ).json()["code"]
        == 20011
    )
    # 一行都没被改：越权不仅要回错误码，还不能留下副作用。
    assert console.rows()["quality"].rerank is True


def test_a_write_without_the_anti_csrf_header_is_forbidden(console: _Harness) -> None:
    console.login("admin", ADMIN_PASSWORD)
    assert (
        console.client.patch("/console/profile-presets/speed", json={"top_k": 8}).json()["code"]
        == 20011
    )
    assert console.rows()["speed"].top_k == 5


# ────────────────────────────────────────────────────────────── 读


def test_listing_returns_the_four_profiles_in_a_fixed_order(console: _Harness) -> None:
    console.login("admin", ADMIN_PASSWORD)
    items = console.data(console.client.get("/console/profile-presets"))["items"]
    assert [item["profile"] for item in items] == ["speed", "balanced", "quality", "custom"]
    # 字段逐项点名，不靠 `**spec` 展开：漏一个字段就是可见的失败。
    assert items[2] == {
        "profile": "quality",
        "mode": "hybrid",
        "top_k": 5,
        "rerank": True,
        "rewrite": True,
        "orchestration": False,
    }


@pytest.mark.parametrize("console", [[]], indirect=True)
def test_an_empty_table_falls_back_to_the_built_in_seed(console: _Harness) -> None:
    """表空（迁移没跑 / 行被删）时回落种子，而不是让接口 500 或返回空列表。"""

    console.login("admin", ADMIN_PASSWORD)
    items = console.data(console.client.get("/console/profile-presets"))["items"]
    assert {item["profile"]: item["top_k"] for item in items} == {key: 5 for key in DEFAULT_PRESETS}


# ────────────────────────────────────────────────────────────── 写


def test_patching_one_field_only_touches_that_field(console: _Harness) -> None:
    console.login("admin", ADMIN_PASSWORD)
    response = console.client.patch(
        "/console/profile-presets/quality", json={"top_k": 9}, headers=WRITE_HEADERS
    )
    data = console.data(response)
    assert data["changed"] == ["top_k"]
    assert data["before"]["top_k"] == 5
    assert data["after"] == {
        "profile": "quality",
        "mode": "hybrid",
        "top_k": 9,
        "rerank": True,
        "rewrite": True,
        "orchestration": False,
    }
    # 只动一个字段：另外三项必须原样（`None` 的语义是「这次不动它」）。
    row = console.rows()["quality"]
    assert (row.mode, row.rerank, row.rewrite) == ("hybrid", True, True)
    assert row.top_k == 9


def test_a_patch_writes_an_audit_row_with_before_and_after(console: _Harness) -> None:
    console.login("admin", ADMIN_PASSWORD)
    console.client.patch(
        "/console/profile-presets/speed", json={"mode": "hybrid"}, headers=WRITE_HEADERS
    )
    entries = console.audit_entries()
    assert len(entries) == 1
    assert entries[0].action == ACTION_PROFILE_UPDATE
    assert entries[0].target == "profile:speed"
    assert entries[0].before_snapshot is not None
    assert entries[0].after_snapshot is not None
    assert entries[0].before_snapshot["mode"] == "vector"
    assert entries[0].after_snapshot["mode"] == "hybrid"


def test_patching_an_unknown_profile_is_a_parameter_error(console: _Harness) -> None:
    console.login("admin", ADMIN_PASSWORD)
    response = console.client.patch(
        "/console/profile-presets/turbo", json={"top_k": 8}, headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 20015


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"mode": "bm25-typo"}, "检索方式不在词表"),
        ({"top_k": 0}, "条数低于下界"),
        ({"top_k": 51}, "条数高于上界"),
    ],
)
def test_patching_an_out_of_range_value_is_a_parameter_error(
    console: _Harness, body: dict[str, Any], why: str
) -> None:
    console.login("admin", ADMIN_PASSWORD)
    response = console.client.patch(
        "/console/profile-presets/speed", json=body, headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 20015, why


# ───────────────────────────────────────────────────── 注册表热更新（本组重点）


def test_a_patch_is_visible_to_the_next_retrieval(console: _Harness) -> None:
    """**后台改完立刻生效**：注册表必须在 PATCH 返回之前就换掉。

    这条用例盯的是「响应说改了、实际检索还按旧参数跑」这类无法自证的错误——它不会报，
    只会让 quality 看起来没变好。
    """

    console.login("admin", ADMIN_PASSWORD)
    assert get_profile_presets()["quality"].rerank is True

    console.client.patch(
        "/console/profile-presets/quality",
        json={"rerank": False, "rewrite": False},
        headers=WRITE_HEADERS,
    )

    # 注册表已换：检索链路展开出来的链路随之改变。
    assert get_profile_presets()["quality"].rerank is False
    snapshot = KeyPolicySnapshot(
        allowed_kb_ids=(),
        research_allowed=False,
        hybrid_allowed=True,
        rerank_allowed=True,
        query_rewrite_allowed=True,
        orchestration_allowed=False,
        retrieve_qps=10,
        retrieve_daily=5000,
        profile="quality",
        include_parent=True,
        expand_table_rows=True,
    )
    chain = profile_chain_of(snapshot)
    assert (chain.mode, chain.top_k, chain.rerank, chain.rewrite) == ("hybrid", 5, False, False)


def test_the_built_in_seed_matches_the_migration_and_the_registry(console: _Harness) -> None:
    """三处必须一致：迁移播种行、内置种子、改造前的硬编码链路。

    不一致的后果是「灌了库」与「没灌库」两种世界跑出不同结果，而这种差异只在某个
    环境里出现一次、之后再也复现不了。
    """

    by_profile = {row.profile: row for row in _seeded_rows()}
    for profile, chain in SEED_PROFILE_CHAINS.items():
        row = by_profile[profile]
        assert (row.mode, row.top_k, row.rerank, row.rewrite) == (
            chain.mode,
            chain.top_k,
            chain.rerank,
            chain.rewrite,
        ), profile
    for profile, spec in DEFAULT_PRESETS.items():
        row = by_profile[profile]
        assert (row.mode, row.top_k, row.rerank, row.rewrite) == (
            spec.mode,
            spec.top_k,
            spec.rerank,
            spec.rewrite,
        ), profile


def test_custom_is_not_read_by_the_retrieval_chain(console: _Harness) -> None:
    """``custom`` 行只给编辑器当初值，检索时仍逐键回落部署配置。

    这条容易在重构里被「顺手统一」掉：一旦 custom 也去读预设，改档位预设就会悄悄改变
    custom 的行为，而用户从界面上看不出来（他填的参数都在，只是「没填的那几项」变了）。
    这里刻意把 custom 预设的条数改成一个明显不等于部署缺省的值，再验检索链路没用它。
    """

    from app.core.profile_presets import set_profile_presets

    set_profile_presets(
        {
            "custom": ProfilePresetSpec(
                mode="vector", top_k=17, rerank=False, rewrite=False, orchestration=False
            )
        }
    )
    snapshot = KeyPolicySnapshot(
        allowed_kb_ids=(),
        research_allowed=False,
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        orchestration_allowed=False,
        retrieve_qps=10,
        retrieve_daily=5000,
        profile="custom",
        include_parent=True,
        expand_table_rows=True,
        # 只给了 mode：其余三项应当回落部署配置，而不是读 custom 那行预设。
        custom_options=(("mode", "hybrid"),),
    )
    chain = profile_chain_of(snapshot)
    assert chain.mode == "hybrid"
    assert chain.top_k == settings.TOP_K
    assert chain.top_k != 17
