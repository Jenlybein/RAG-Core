"""控制台「最近检索」列表与观测配置的验收。

两条端点都是**新补的入口**（原来只能按 id 单条查），所以这里钉住三件事：

1. **只给本租户**：A 租户的会话看不到 B 租户的行——这是 ADR-0006 在列表查询上的落点，
   与点查同样严格（点查靠 `tenant_id` 条件，列表靠同一个条件）。
2. **未评分是 `None`，不是 0**：列表页据此画「还没人评」，画成 0 星是另一种意思。
3. **观测栈地址只在去向真是 `langfuse` 时下发**：去向是 `jsonl` / `none` 时栈里没有这条
   trace，给地址只会点出 404。

接缝是 ASGI 边界（真请求 + 真登录），与 `test_console_trace_lookup.py` 同一套理由：
直接调函数会绕过路由声明，而「路径参数写成 query」这类错法只有发真请求才暴露。

**两个会话替身是双栈期的显式事实**（第 11 步票 #128）：控制面身份仍走同步 `get_db`，
「最近检索」这条读已经走异步 `get_async_db`；两条都注入内存替身。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.config import settings
from app.models.account import Account
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.retrieval_trace import RetrievalTrace
from app.models.tenant import Tenant
from app.services.trace_identity import trace_id_for
from tests.fake_db import async_mirror, RecordingSession

LOGIN_PATH = "/console/login"
RECENT_PATH = "/console/retrieval-traces/recent"
RUNTIME_INFO_PATH = "/console/runtime-info"
USER_PASSWORD = "user-password"
TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _account(account_id: str, username: str, tenant_id: str) -> Account:
    return Account(
        id=account_id,
        tenant_id=tenant_id,
        username=username,
        password_hash=passwords.hash_password(USER_PASSWORD),
        role="user",
        status="active",
    )


def _trace(log_id: str, tenant_id: str, *, degraded: str | None = None) -> RetrievalTrace:
    return RetrievalTrace(
        id=log_id,
        tenant_id=tenant_id,
        trace_id=trace_id_for(log_id),
        kb_ids=["kb-a1", "kb-a2"],
        chunk_ids=["chunk-a1"],
        top_k=5,
        latency_ms=123,
        degraded_reason=degraded,
        empty_reason=None,
        created_at=datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc),
    )


def _world() -> dict[type, list[Any]]:
    return {
        Plan: _plan_rows(),
        Tenant: [
            Tenant(id=TENANT_ALPHA, name="Alpha", plan_id=3, status="active"),
            Tenant(id=TENANT_BETA, name="Beta", plan_id=3, status="active"),
        ],
        Account: [
            _account(ALICE_ID, "alice", TENANT_ALPHA),
            _account(BOB_ID, "bob", TENANT_BETA),
        ],
        RetrievalTrace: [],
    }


@dataclass
class _Harness:
    client: TestClient

    def login(self, username: str) -> Any:
        return self.client.post(LOGIN_PATH, json={"username": username, "password": USER_PASSWORD})

    def recent(self, limit: int = 20) -> Any:
        return self.client.get(RECENT_PATH, params={"limit": limit})

    def runtime_info(self) -> Any:
        return self.client.get(RUNTIME_INFO_PATH)


@pytest.fixture
def console() -> Iterator[_Harness]:
    main = importlib.import_module("app.main")
    session_module = importlib.import_module("app.db.session")
    async_session = importlib.import_module("app.db.async_session")
    console_deps = importlib.import_module("app.api.console_deps")

    # **一个同步会话实例贯穿整个用例**：登录会把会话行写进这个假库，后续请求要能读回同一行。
    # 每次请求新建一个 ``RecordingSession`` 会让登录「成功」而下一个请求 20010，
    # 排查时看着像 cookie 没带上，实际是库被换掉了。
    session = RecordingSession(rows=_world())
    # 「最近检索」那条读（含身份判定）走异步会话；两个替身共享同一份行，于是登录写下的
    # 会话行在只读请求里读得到。
    trace_session = async_mirror(session)
    main.app.dependency_overrides[session_module.get_db] = lambda: session
    main.app.dependency_overrides[async_session.get_async_db] = lambda: trace_session
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: None
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client)
    finally:
        main.app.dependency_overrides.clear()


def _stub_repositories(
    monkeypatch: pytest.MonkeyPatch, *, rows: list[RetrievalTrace], scores: dict[str, int]
) -> dict[str, Any]:
    """把两个仓储换成记录参数的桩：这一层要验的是「路由怎么调仓储」，不是 SQL。

    桩是 ``async def``：两个读法已经换到 ``AsyncSession`` 上（票 #128 起），同步桩会被
    路由 ``await`` 到但永远拿不到结果——那样失败的是「没 await」，不是这张票要守的行为。
    """

    trace_repo = importlib.import_module("app.repositories.retrieval_trace_repository")
    feedback_repo = importlib.import_module("app.repositories.retrieval_feedback_repository")
    seen: dict[str, Any] = {}

    async def _list_recent(db: Any, *, tenant_id: str, limit: int) -> list[RetrievalTrace]:
        seen["tenant_id"] = tenant_id
        seen["limit"] = limit
        return rows

    async def _scores(db: Any, log_ids: Any, *, tenant_id: str) -> dict[str, int]:
        seen["score_tenant_id"] = tenant_id
        seen["score_log_ids"] = list(log_ids)
        return scores

    monkeypatch.setattr(trace_repo, "list_recent_for_tenant", _list_recent)
    monkeypatch.setattr(feedback_repo, "scores_for_log_ids", _scores)
    return seen


def test_recent_lists_only_the_callers_tenant_with_its_scores(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_trace("log-alpha-0001", TENANT_ALPHA, degraded="keyword search failed")]
    seen = _stub_repositories(monkeypatch, rows=rows, scores={"log-alpha-0001": 2})

    assert console.login("alice").json()["code"] == 0
    body = console.recent(limit=5).json()

    assert body["code"] == 0
    # 租户只来自会话：路由把会话里的 tenant_id 交给仓储，不读请求参数。
    assert seen["tenant_id"] == TENANT_ALPHA
    assert seen["limit"] == 5
    assert seen["score_tenant_id"] == TENANT_ALPHA
    assert seen["score_log_ids"] == ["log-alpha-0001"]

    item = body["data"]["items"][0]
    assert item["log_id"] == "log-alpha-0001"
    assert item["score"] == 2
    assert item["degraded_reason"] == "keyword search failed"
    assert item["kb_count"] == 2
    assert item["latency_ms"] == 123
    # 响应里**没有原文**：归属索引不存，列表页也就不可能把它画出来。
    assert "query" not in item


def test_an_unrated_trace_reports_a_null_score_not_zero(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未评分与 0 分是两件事：1~5 的取值域里没有 0。"""

    _stub_repositories(monkeypatch, rows=[_trace("log-alpha-0002", TENANT_ALPHA)], scores={})

    assert console.login("alice").json()["code"] == 0
    item = console.recent().json()["data"]["items"][0]

    assert item["score"] is None


def test_recent_rejects_a_limit_outside_the_allowed_range(console: _Harness) -> None:
    """`limit` 有上限：没有上限的列表查询在表长大之后会变成一次全表扫。"""

    assert console.login("alice").json()["code"] == 0

    assert console.recent(limit=0).json()["code"] == 20002
    assert console.recent(limit=101).json()["code"] == 20002


def test_runtime_info_reports_no_host_when_the_sink_is_not_langfuse(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "http://localhost:3000")

    assert console.login("alice").json()["code"] == 0
    data = console.runtime_info().json()["data"]

    assert data["trace_sink"] == "jsonl"
    # 去向不是 langfuse 时栈里没有这条 trace，给地址只会点出 404。
    assert data["langfuse_host"] is None


def test_runtime_info_prefers_the_public_url_for_the_browser(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器里 `LANGFUSE_HOST` 是服务名，浏览器解析不了——对外地址必须单独给。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_URL", "http://localhost:3000")

    assert console.login("alice").json()["code"] == 0
    data = console.runtime_info().json()["data"]

    assert data["langfuse_host"] == "http://localhost:3000"
    # 密钥一个都不出这个进程。
    assert "key" not in str(data).lower()


def test_runtime_info_falls_back_to_host_when_no_public_url(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "http://localhost:3000/")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_URL", "")

    assert console.login("alice").json()["code"] == 0
    # 末尾斜杠要去掉，否则前端拼出来是 //traces/xxx。
    assert console.runtime_info().json()["data"]["langfuse_host"] == "http://localhost:3000"


def test_runtime_info_reports_whether_real_keys_are_enforced(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auth_enabled` 必须如实下发：界面据它在**试之前**说明「控制台的 Key 不生效」。

    真实反馈：服务端 `AUTH_ENABLED=false`，使用者从控制台选了 Key 去检索，得到 20010
    「Key 无效」，于是反复重签——而真正要改的是服务端开关。这条字段就是让界面能提前说清。
    """

    monkeypatch.setattr(settings, "AUTH_ENABLED", False)
    assert console.login("alice").json()["code"] == 0
    assert console.runtime_info().json()["data"]["auth_enabled"] is False

    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    assert console.runtime_info().json()["data"]["auth_enabled"] is True
