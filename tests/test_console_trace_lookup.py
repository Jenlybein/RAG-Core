"""控制面「按 trace_id 反查 log_id」的验收（#106）。

**接缝是 ASGI 边界，不是函数**：真实应用 + 真实 ``POST /console/login`` 拿到的会话
cookie，之后只看统一响应体。这条是 code-review 之后改过来的——上一版直接调端点函数，
于是「``trace_id`` 被声明成路径参数、而路由里没有那个路径段」这种错法**整套离线测试
仍然全绿**（函数签名照收、仓储照调），而真实调用一律 20002。教训写在 `tests/README.md`
② 层：这一层必须发真请求。

仓库既有口径在这里同样适用：数据库用 ``dependency_overrides`` 换成内存会话替身、
观测服务用假客户端，因此不连 PG、不起栈、不发网络。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.models.account import Account
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.retrieval_trace import RetrievalTrace
from app.models.tenant import Tenant
from app.services.trace_identity import trace_id_for
from tests.fake_db import async_mirror, RecordingSession

LOGIN_PATH = "/console/login"
LOOKUP_PATH = "/console/retrieval-traces"
USER_PASSWORD = "user-password"
TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"
LOG_ALPHA = "log-alpha-0001"


class _TraceRows(RecordingSession):
    """认得归属索引那条 ``select(...).where(trace_id == ?, tenant_id == ?)`` 的假会话。

    不 patch 仓储：让真实 SQL 构造跑一遍，只把执行结果换成内存行。替身只认**这一种**
    查询形状——假会话越像真会话，越容易让人误以为测过的是数据库。
    """

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        table = _final_table_name(statement)
        if table == RetrievalTrace.__tablename__:
            rows = [row for row in self._rows.get(RetrievalTrace, []) if _matches(row, statement)]
            return _SelectResult(rows)
        return super().execute(statement, *args, **kwargs)


class _SelectResult:
    """``db.execute(select(...))`` 的最小替身：仓储只要 ``scalar_one_or_none()``。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


def _final_table_name(statement: Any) -> str | None:
    try:
        return statement.get_final_froms()[0].name
    except (AttributeError, IndexError):
        return None


def _matches(row: Any, statement: Any) -> bool:
    """只按等值 ``col = 字面量`` 比对——这条查询就两个条件，多一种形状说明实现变了。"""

    criterion = getattr(statement, "whereclause", None)
    pending = [criterion] if criterion is not None else []
    while pending:
        node = pending.pop()
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        if left is not None and hasattr(left, "key") and hasattr(right, "value"):
            if getattr(row, left.key) != right.value:
                return False
            continue
        clauses = getattr(node, "clauses", None)
        if clauses:
            pending.extend(clauses)
    return True


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


def _trace(log_id: str, tenant_id: str) -> RetrievalTrace:
    return RetrievalTrace(
        id=log_id,
        tenant_id=tenant_id,
        trace_id=trace_id_for(log_id),
        kb_ids=["kb-a1"],
        chunk_ids=["chunk-a1"],
        top_k=5,
        latency_ms=120,
        degraded_reason=None,
        empty_reason=None,
        created_at=None,
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
        RetrievalTrace: [
            _trace(LOG_ALPHA, TENANT_ALPHA),
            _trace("log-beta-0001", TENANT_BETA),
        ],
    }


@dataclass
class _Harness:
    client: TestClient

    def login(self, username: str) -> Any:
        return self.client.post(LOGIN_PATH, json={"username": username, "password": USER_PASSWORD})

    def lookup(self, trace_id: str) -> Any:
        return self.client.get(LOOKUP_PATH, params={"trace_id": trace_id})


@pytest.fixture
def console() -> Iterator[_Harness]:
    main = importlib.import_module("app.main")
    session_module = importlib.import_module("app.db.session")
    async_session = importlib.import_module("app.db.async_session")
    console_deps = importlib.import_module("app.api.console_deps")

    session = _TraceRows(rows=_world())
    main.app.dependency_overrides[session_module.get_db] = lambda: session
    # 反查走异步会话（票 #129），替身与同步那个共享同一份行。
    main.app.dependency_overrides[async_session.get_async_db] = lambda: async_mirror(session)
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: None
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client)
    finally:
        main.app.dependency_overrides.clear()


def test_a_known_trace_resolves_to_its_log_id(console: _Harness) -> None:
    """真请求、真会话、真参数校验：`trace_id` 必须是 query 参数。"""

    assert console.login("alice").json()["code"] == 0

    body = console.lookup(trace_id_for(LOG_ALPHA)).json()

    assert body["code"] == 0, body
    assert body["data"]["log_id"] == LOG_ALPHA
    assert body["data"]["trace_id"] == trace_id_for(LOG_ALPHA)


def test_the_parameter_is_a_query_parameter_not_a_path_segment(console: _Harness) -> None:
    """回归：把 `trace_id` 声明成 `Path` 时这条会红。

    `Path` 会让 FastAPI 期待一个路由里不存在的路径段，真实调用变 20002
    （`loc: ["path", "trace_id"]`）——而直接调函数的测试察觉不到。
    """

    assert console.login("alice").json()["code"] == 0

    response = console.lookup(trace_id_for(LOG_ALPHA))

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0, body
    assert "path" not in (body.get("data") or {})


def test_another_tenants_trace_resolves_to_nothing(console: _Harness) -> None:
    """跨租户与不存在**同一个答复**：不泄漏「这条 trace 在别人那儿存在」。"""

    assert console.login("alice").json()["code"] == 0

    body = console.lookup(trace_id_for("log-beta-0001")).json()

    assert body["code"] == 0, body
    assert body["data"]["log_id"] is None


def test_an_unknown_trace_resolves_to_nothing(console: _Harness) -> None:
    assert console.login("alice").json()["code"] == 0

    body = console.lookup("c" * 32).json()

    assert body["code"] == 0 and body["data"]["log_id"] is None


@pytest.mark.parametrize("trace_id", ["../../etc/passwd", "abc", "a" * 33, "z" * 32])
def test_an_illegal_shape_is_treated_as_absent(console: _Harness, trace_id: str) -> None:
    """形状不对就是「不存在」，不把任意一串当查询打进去。"""

    assert console.login("alice").json()["code"] == 0

    body = console.lookup(trace_id).json()

    assert body["code"] == 0, body
    assert body["data"]["log_id"] is None


def test_uppercase_trace_id_is_normalized(console: _Harness) -> None:
    """`trace_id` 是十六进制串：大小写不该决定「查不查得到」。"""

    assert console.login("alice").json()["code"] == 0

    body = console.lookup(trace_id_for(LOG_ALPHA).upper()).json()

    assert body["code"] == 0, body
    assert body["data"]["trace_id"] == trace_id_for(LOG_ALPHA)


def test_a_missing_parameter_is_a_parameter_error(console: _Harness) -> None:
    """缺参数是 20002（参数校验失败），不是「不存在」——两者对调用方是不同的事。"""

    assert console.login("alice").json()["code"] == 0

    body = console.client.get(LOOKUP_PATH).json()

    assert body["code"] == 20002, body
