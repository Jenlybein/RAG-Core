"""票 #103：控制台按 ``log_id`` 读回六段瀑布的验收。

接缝是一条：真实应用 + ``TestClient`` + 真实 ``POST /console/login`` 拿到的会话 cookie。
之后只看统一响应体。归属索引那一段用内存会话替身（``_SelectResult`` 认得仓储真正发出的
那条 ``select``），JSONL 用**真实写入**（``emit_retrieval_trace`` → ``JsonlTraceSink``），
观测服务用**假客户端**。因此这里既不需要 PostgreSQL，也不需要起观测栈，更不发一次网络。

钉住四件事，每一件都是票面的验收项：

1. 拿得到六段，顺序是链路顺序（不是写入顺序）；
2. 跨租户的 ``log_id`` 与不存在的 ``log_id`` **同码**（20016），不是 403；
3. 两处都没有时是**结构化**的「未启用 / 无记录」，不是 404、也不是空数组；
4. 响应里不出现观测凭据，且观测服务不可达时仍然 200（降级成「只有本地结果」+ 一句说明）。

另外钉住两条容易被「优化」掉的性质：本地命中时**绝不去问观测服务**（替身会当场抛），
以及「关闭观测 + 本地有记录」仍然把瀑布画得出来（``enabled=false`` 但 ``stages`` 非空）。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import passwords
from app.core.config import settings
from app.models.account import Account
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.retrieval_trace import RetrievalTrace
from app.models.tenant import Tenant
from app.observability import emit_retrieval_trace, set_trace_sink
from app.observability.jsonl_sink import JsonlTraceSink
from app.observability.langfuse_reader import (
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    LangfuseReadError,
    read_stage_spans,
)
from app.observability.stage_timing import (
    STAGE_COLLAPSE,
    STAGE_FUSION,
    STAGE_KEYWORD_RECALL,
    STAGE_QUERY_OPTIMIZATION,
    STAGE_RERANK,
    STAGE_VECTOR_RECALL,
    StageSpan,
)
from app.services.trace_identity import trace_id_for
from tests.fake_db import RecordingSession

WATERFALL_PATH = "/console/retrieval-traces/{log_id}/waterfall"
LOGIN_PATH = "/console/login"

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"
BOB_ID = "00000000-0000-4000-8000-000000000003"
ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"
TENANT_BETA = "tenant_beta"

#: 三次检索：Alpha 一次、Beta 一次，外加一个「谁都没有」的 id（形状与 UUID v4 一致）。
LOG_ALPHA = "11111111-1111-4111-8111-111111111111"
LOG_BETA = "22222222-2222-4222-8222-222222222222"
LOG_MISSING = "33333333-3333-4333-8333-333333333333"

#: 六段耗时：故意写成非递增，好让「按链路顺序」与「按耗时」区分开（混起来会画错瀑布）。
ALPHA_STAGES = (
    StageSpan(STAGE_QUERY_OPTIMIZATION, 3.5),
    StageSpan(STAGE_VECTOR_RECALL, 41.25),
    StageSpan(STAGE_KEYWORD_RECALL, 12.0),
    StageSpan(STAGE_FUSION, 1.5),
    StageSpan(STAGE_RERANK, 88.437),
    StageSpan(STAGE_COLLAPSE, 2.0),
)

#: 观测凭据的哨兵：出现在响应里就说明「密钥不下发浏览器」这条破了。
SECRET_SENTINEL = "T103-SECRET-SENTINEL-0d4c"
PUBLIC_SENTINEL = "T103-PUBLIC-SENTINEL-7a91"


# ────────────────────────────────────────────────────────────────── 离线替身


class _SelectResult:
    """``db.execute(select(...))`` 的最小替身：仓储只要一个 ``scalar_one_or_none()``。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _WaterfallSession(RecordingSession):
    """``RecordingSession`` + 「认得归属索引那条 select」。

    不 patch 仓储函数：让真实 SQL 构造跑一遍，只把执行结果换成内存行。替身故意只认这一种查询
    形状（``WHERE id = ? AND tenant_id = ?``），别的语句仍落到父类的空结果——假会话越像真
    会话，越容易让人误以为测过的是数据库。
    """

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if _final_table_name(statement) == RetrievalTrace.__tablename__:
            rows = [
                row
                for row in self._rows.get(RetrievalTrace, [])
                if _matches(row, statement)
            ]
            return _SelectResult(rows)
        return super().execute(statement, *args, **kwargs)


def _final_table_name(statement: Any) -> str | None:
    try:
        return statement.get_final_froms()[0].name
    except (AttributeError, IndexError):
        return None


def _matches(row: Any, statement: Any) -> bool:
    """按 ``where`` 里的每一对「列 = 字面量」过滤；认不出形状就当不匹配。"""

    whereclause = getattr(statement, "whereclause", None)
    if whereclause is None:
        return True
    clauses = getattr(whereclause, "clauses", None) or (whereclause,)
    for clause in clauses:
        original = getattr(clause, "left", None)
        literal = getattr(clause, "right", None)
        if original is None or not hasattr(literal, "value"):
            return False
        if getattr(row, original.key) != literal.value:
            return False
    return True


class _MemoryPlanCache:
    """内存假套餐缓存：套餐解析只用到 ``get / set / delete``。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, plan: Any) -> None:
        self.items[plan_id] = plan

    def delete(self, plan_id: int) -> None:
        self.items.pop(plan_id, None)


@dataclass
class _FakeObservations:
    """假观测服务：记下问了哪个 ``trace_id``，再决定「返回 / 抛 / 炸」。"""

    stages: dict[str, list[StageSpan]] = field(default_factory=dict)
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def __call__(self, trace_id: str) -> list[StageSpan]:
        self.calls.append(trace_id)
        if self.error is not None:
            raise self.error
        return self.stages.get(trace_id, [])


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: _WaterfallSession
    reader: _FakeObservations
    jsonl_dir: Path

    def login(self, username: str, password: str) -> Any:
        return self.client.post(LOGIN_PATH, json={"username": username, "password": password})

    def fetch(self, log_id: str) -> Any:
        return self.client.get(WATERFALL_PATH.format(log_id=log_id))

    def data(self, log_id: str) -> Any:
        body = self.fetch(log_id).json()
        assert body["code"] == 0, body
        return body["data"]


# ──────────────────────────────────────────────────────────────────── 世界


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _account(
    account_id: str, username: str, password: str, *, role: str = "user", tenant_id: str | None = None
) -> Account:
    return Account(
        id=account_id,
        tenant_id=tenant_id,
        username=username,
        password_hash=passwords.hash_password(password),
        role=role,
        status="active",
    )


def _trace(log_id: str, tenant_id: str, *, latency_ms: int = 315) -> RetrievalTrace:
    return RetrievalTrace(
        id=log_id,
        tenant_id=tenant_id,
        trace_id=trace_id_for(log_id),
        kb_ids=["kb-a1"],
        chunk_ids=["chunk-a1"],
        top_k=5,
        latency_ms=latency_ms,
        degraded_reason=None,
        empty_reason=None,
        created_at=datetime(2026, 11, 10, 9, 30, 0),
    )


def _world() -> dict[type, list[Any]]:
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
        # 归属索引里有 Alpha 与 Beta 各一行：跨租户用例要的是「行真的在，但不属于你」。
        RetrievalTrace: [
            _trace(LOG_ALPHA, TENANT_ALPHA, latency_ms=315),
            _trace(LOG_BETA, TENANT_BETA, latency_ms=900),
        ],
    }


@pytest.fixture
def console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    """真实应用 + 真实登录 + 内存会话 + 内存 JSONL 目录 + 假观测服务。"""

    main = importlib.import_module("app.main")
    session_module = importlib.import_module("app.db.session")
    console_deps = importlib.import_module("app.api.console_deps")

    jsonl_dir = tmp_path / "traces"
    monkeypatch.setattr(settings, "TRACE_JSONL_DIR", str(jsonl_dir))
    monkeypatch.setattr(settings, "TRACE_JSONL_RETENTION_DAYS", 7)
    # 缺省「观测没开」：观测服务的替身在这条路径上不该被调用（用例自己会断言）。
    monkeypatch.setattr(settings, "TRACE_SINK", "none")

    session = _WaterfallSession(rows=_world())
    cache = _MemoryPlanCache()
    reader = _FakeObservations()

    main.app.dependency_overrides[session_module.get_db] = lambda: session
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    main.app.dependency_overrides[console_deps.get_langfuse_reader] = lambda: reader
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=session, reader=reader, jsonl_dir=jsonl_dir)
    finally:
        main.app.dependency_overrides.clear()


def _write_local_record(trace_id: str, stages: tuple[StageSpan, ...] = ALPHA_STAGES) -> None:
    """用**真实写入路径**在本地 JSONL 里造一条记录（去向是临时目录里的文件）。"""

    set_trace_sink(
        JsonlTraceSink(settings.TRACE_JSONL_DIR, retention_days=settings.TRACE_JSONL_RETENTION_DAYS)
    )
    emit_retrieval_trace(
        trace_id=trace_id,
        spans=list(stages),
        attributes={"mode": "hybrid", "top_k": 5},
    )


# ─────────────────────────────────────────────────────── ① 六段：本地记录优先


def test_local_record_returns_the_six_stages_in_link_order(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地有行 → 六段名称与耗时都拿得到，顺序是链路顺序，且**不问**观测服务。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    _write_local_record(trace_id_for(LOG_ALPHA))
    console.reader.error = AssertionError("本地命中时不该去问观测服务")
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    data = console.data(LOG_ALPHA)

    assert data["log_id"] == LOG_ALPHA
    assert data["trace_id"] == trace_id_for(LOG_ALPHA)
    assert data["source"] == "jsonl"
    assert data["reason"] is None
    assert data["note"] is None
    assert data["enabled"] is True
    assert [(stage["name"], stage["duration_ms"]) for stage in data["stages"]] == [
        (stage.name, stage.duration_ms) for stage in ALPHA_STAGES
    ]
    # 归属索引里那一列原样回显：瀑布旁边要写「这次一共多慢」，不能拿六段之和冒充。
    assert data["latency_ms"] == 315
    assert data["created_at"] is not None
    assert console.reader.calls == []


def test_a_disabled_sink_still_serves_the_local_record(console: _Harness) -> None:
    """关闭观测（``TRACE_SINK=none``）时，早先写下的本地记录照样看得到。

    ``enabled`` 说的是**当前**配置，所以这里是 ``false`` + 六段都在——前端不能因为
    ``enabled=false`` 就把瀑布整块藏掉。
    """

    _write_local_record(trace_id_for(LOG_ALPHA))
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    data = console.data(LOG_ALPHA)

    assert data["enabled"] is False
    assert data["source"] == "jsonl"
    assert len(data["stages"]) == 6
    assert data["reason"] is None
    assert console.reader.calls == []


def test_only_the_owners_tenant_reads_a_record(console: _Harness) -> None:
    """Alpha 的会话看 Beta 的 ``log_id``：同码 20016，且没有读到任何段。"""

    _write_local_record(trace_id_for(LOG_BETA), ALPHA_STAGES)
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    body = console.fetch(LOG_BETA).json()

    assert body["code"] == 20016
    assert body["data"] is None
    assert console.reader.calls == []


# ─────────────────────────────────────────── ② 两处都没有：结构化「未启用 / 无记录」


def test_no_record_anywhere_is_structured_not_an_empty_array(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观测开着（去向 jsonl）但这条没记上 → ``no_record``，而且**问过**观测服务。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    body = console.fetch(LOG_ALPHA).json()

    assert body["code"] == 0, "「没记上」不是错误，而是结构化结果"
    data = body["data"]
    assert data["enabled"] is True
    assert data["reason"] == "no_record"
    assert data["source"] is None
    assert data["stages"] == []
    # 本地没命中时才轮到观测服务：这次它被问了一次，回答是空。
    assert console.reader.calls == [trace_id_for(LOG_ALPHA)]


def test_a_disabled_sink_without_a_local_record_says_not_enabled(console: _Harness) -> None:
    """关闭观测且无本地记录 → ``sink_disabled``，且不去打观测服务。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    data = console.data(LOG_ALPHA)

    assert data["enabled"] is False
    assert data["reason"] == "sink_disabled"
    assert data["note"] is None
    assert data["stages"] == []
    assert console.reader.calls == []


# ───────────────────────────────────────────────────── ③ 观测服务：服务端读、不外泄


def test_the_observation_service_is_read_on_the_server_and_ordered(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地没命中 → 让真实读数逻辑跑一遍假客户端：只留六段、按链路顺序、耗时取 metadata。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    stages = read_stage_spans(
        trace_id_for(LOG_ALPHA),
        client=_fake_langfuse_client(
            [
                # 故意乱序，并混进一个不属于六段的 observation。
                ("collapse", 2.0),
                ("someone-elses-span", 999.0),
                ("vector_recall", 41.25, None, None),
                ("rerank", None, datetime(2026, 11, 10, 9, 0, 1), datetime(2026, 11, 10, 9, 0, 1, 88000)),
                ("query_optimization", 3.5, None, None),
                ("keyword_recall", 12.0, None, None),
                ("fusion", 1.5, None, None),
            ]
        ),
    )
    console.reader.stages = {trace_id_for(LOG_ALPHA): list(stages)}
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    data = console.data(LOG_ALPHA)

    assert data["source"] == "langfuse"
    assert data["reason"] is None
    assert [stage["name"] for stage in data["stages"]] == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_VECTOR_RECALL,
        STAGE_KEYWORD_RECALL,
        STAGE_FUSION,
        STAGE_RERANK,
        STAGE_COLLAPSE,
    ]
    assert data["stages"][4]["duration_ms"] == pytest.approx(88.0)


def test_an_unreachable_observation_service_still_answers_with_a_note(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观测服务读不到 → 200、``no_record`` + 一句说明；绝不 500。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    console.reader.error = LangfuseReadError(REASON_UNREACHABLE)
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    body = console.fetch(LOG_ALPHA).json()

    assert body["code"] == 0
    data = body["data"]
    assert data["enabled"] is True
    assert data["reason"] == "no_record"
    assert data["stages"] == []
    assert data["note"] is not None
    assert REASON_UNREACHABLE in data["note"]


def test_the_response_never_carries_observability_credentials(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观测凭据**不下发浏览器**：任何一次响应里都找不到它们。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", PUBLIC_SENTINEL)
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", SECRET_SENTINEL)
    _write_local_record(trace_id_for(LOG_ALPHA))
    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    bodies = [
        console.fetch(LOG_ALPHA).text,  # 有段
        console.fetch(LOG_MISSING).text,  # 不存在
    ]
    for text in bodies:
        assert SECRET_SENTINEL not in text
        assert PUBLIC_SENTINEL not in text
        assert "LANGFUSE" not in text


# ──────────────────────────────────────────────────────────── ④ 门禁与越界口径


@pytest.mark.parametrize("log_id", [LOG_BETA, LOG_MISSING], ids=["cross-tenant", "missing"])
def test_another_tenant_or_a_missing_log_id_is_not_found(
    console: _Harness, log_id: str
) -> None:
    """跨租户与不存在同码同文案：可区分的拒绝会泄露存在性。"""

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    body = console.fetch(log_id).json()

    assert body["code"] == 20016
    assert body["msg"] == console.fetch(LOG_MISSING).json()["msg"]


def test_the_waterfall_requires_a_console_session(console: _Harness) -> None:
    body = console.fetch(LOG_ALPHA).json()

    assert body["code"] == 20010
    assert body["data"] is None


def test_an_admin_without_a_tenant_is_forbidden(console: _Harness) -> None:
    """管理员不属于任何租户 → 20011（「用错了门」），不是空结果。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    body = console.fetch(LOG_ALPHA).json()

    assert body["code"] == 20011
    assert body["data"] is None


# ──────────────────────────────────────────────────────────── ⑤ 读数逻辑的单元


def _fake_langfuse_client(
    observations: list[tuple[Any, ...]]
) -> Any:
    """构造一个假的 ``LangfuseAPI``：只实现 ``observations.get_many``。"""

    def _observation(entry: tuple[Any, ...]) -> Any:
        name = entry[0]
        duration = entry[1]
        started = entry[2] if len(entry) > 2 else None
        ended = entry[3] if len(entry) > 3 else None
        return SimpleNamespace(
            name=name,
            metadata=None if duration is None else {"duration_ms": duration},
            start_time=started,
            end_time=ended,
        )

    class _Observations:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        def get_many(self, **kwargs: Any) -> Any:
            self.kwargs.update(kwargs)
            return SimpleNamespace(data=[_observation(entry) for entry in observations])

    return SimpleNamespace(observations=_Observations())


def test_reader_asks_the_api_for_name_and_metadata_explicitly() -> None:
    """``fields`` 必须同时点名 ``basic`` 与 ``metadata``，两个都不能少。

    这是实机踩出来的（2026-11-11，自托管 v4）：v2 的 observations 接口**默认不返回
    metadata**，漏了就每段都退回 ``end - start``（事后补记里几乎相同）→ 瀑布图全是
    ``0.0 ms``；而 ``fields`` 一旦传了就是**白名单**，只写 ``metadata`` 会把 ``name``
    滤掉 → 每段都因为「名字不认识」被丢弃 → 瀑布图变成空数组。

    两种坏法都不报错，只看假客户端的行为也发现不了（它照旧把 metadata 塞回来），
    所以这里直接钉住**发出去的请求参数**。
    """

    client = _fake_langfuse_client([("fusion", 1.5)])

    read_stage_spans("0" * 32, client=client)

    fields = client.observations.kwargs.get("fields")
    assert fields is not None, "不传 fields 就永远读不到 metadata.duration_ms"
    assert "metadata" in fields, "不点名 metadata 的话 duration_ms 读不回来"
    assert "basic" in fields, "不点名 basic 的话 name 会被白名单滤掉"


def test_reader_without_credentials_reports_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据没配时**不发请求**：构造客户端那一步都不该发生。"""

    reader_module = importlib.import_module("app.observability.langfuse_reader")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    monkeypatch.setattr(
        reader_module, "_new_client", lambda: pytest.fail("没配凭据时不该构造客户端")
    )

    with pytest.raises(LangfuseReadError) as raised:
        read_stage_spans("0" * 32)

    assert raised.value.reason == REASON_NOT_CONFIGURED


def test_reader_wraps_sdk_failure_without_leaking_the_message() -> None:
    """SDK 的异常文本可能带 host 与认证头，所以只留短码。"""

    class _Broken:
        def get_many(self, **_kwargs: Any) -> Any:
            raise RuntimeError(f"401 for {SECRET_SENTINEL}")

    with pytest.raises(LangfuseReadError) as raised:
        read_stage_spans("0" * 32, client=SimpleNamespace(observations=_Broken()))

    assert raised.value.reason == REASON_UNREACHABLE
    assert SECRET_SENTINEL not in str(raised.value)
    assert SECRET_SENTINEL not in (raised.value.detail or "")



