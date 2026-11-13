"""票 #79 的接线：第五 / 六道关卡（限流 → 日配额）与静态配额在 ASGI 边界上的样子。

两半接缝，刻意分在两个连续的循环里写：

* **检索入口**（HTTP 与 MCP 两条入口挂同一个应用）：网关放在档位门禁之后、业务执行之前；
  成功后两级各记一次，拒绝与失败一次都不记；秒级超限是 ``20005``、额度用尽是 ``20014``。
* **写路径**（建库 / 上传 / 重建）：落行或入队**之前**到库里数行，超限 ``20014``。

默认套件全程离线：身份由依赖覆盖直接给出，计数器注入一个「dict + TTL 账本」的假客户端
（经 ``app.core.redis_client.set_redis_client`` 这唯一注入点），不碰 PostgreSQL、不碰真 Redis。
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.utilities.lifespan import combine_lifespans

from app.core import redis_client
from app.core.auth import DEV_KEY_POLICY, KeyContext
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.models.key_policy import PROFILE_SPEED, KeyPolicySnapshot
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO, PlanSpec
from app.services.quota_gate import QuotaCounters, QuotaWindow, daily_key, qps_key
from app.utils.status import DocumentStatus
from tests import fake_db
from tests.fake_db import AsyncSessionAdapter, RecordingSession

TENANT_ID = "tenant_acme"
KEY_A = "key-a"
KEY_B = "key-b"
KB_A = "kb-a"
KB_B = "kb-b"
KB_C = "kb-c"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
CREATE_KB_PATH = "/api/v1/knowledge-bases/create"
UPLOAD_PATH = "/api/v1/documents/upload"
MCP_PATH = "/mcp/"
JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


# ───────────────────────────────────────────── 离线替身


class _FakeCounterStore:
    """计数器真正用到的那三条命令：一个 dict 加一份 TTL 账本。

    命令是**异步**的（票 #133）：生产客户端是 ``redis.asyncio``，注入缝认的就是这个形状。
    """

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str) -> str | None:
        value = self.values.get(key)
        return None if value is None else str(value)

    async def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


class _ExplodingStore:
    """任何一条命令都炸：用来证明开关关着时一次都没碰 Redis。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def incr(self, key: str) -> int:
        self.calls.append(f"incr:{key}")
        raise AssertionError(f"不该访问 Redis：incr({key})")

    async def get(self, key: str) -> str | None:
        self.calls.append(f"get:{key}")
        raise AssertionError(f"不该访问 Redis：get({key})")

    async def expire(self, key: str, ttl: int) -> None:
        self.calls.append(f"expire:{key}")
        raise AssertionError(f"不该访问 Redis：expire({key}, {ttl})")


class _WriteSession(RecordingSession):
    """共享替身 + 一件真库会做的事：``flush`` 之后补上 ``created_at``。

    读侧沿用共享替身的空结果（检索替身本来就回空元数据查询）；写侧必须留下行，否则
    「被拒的请求没有落行」这条断言就无从谈起。``add`` / ``execute`` / ``commit`` /
    ``rollback`` / ``info`` 全部沿用共享替身——本地假会话每多写一个方法，就多一处
    「跟真货不一致」的机会。
    """

    def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime(2024, 1, 2, 3, 4, 5)


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    async def similarity_search_scope(
        self, _db: Any, _vector: Any, *, tenant_id: str, index_versions: dict, top_k: int
    ) -> list[dict]:
        return [
            {
                "document_id": f"doc-{kb_id}",
                "chunk_id": f"chunk-{kb_id}",
                "kb_id": kb_id,
                "title": "手册",
                "content": f"{kb_id} 的内容",
                "score": 0.9 - index * 0.1,
            }
            for index, kb_id in enumerate(index_versions)
        ]

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        raise AssertionError("范围版一次覆盖全部库，不许按库循环")


# ───────────────────────────────────────────── 身份与限额


def _policy(
    *,
    qps: int = 10,
    daily: int = 100,
    allowed_kb_ids: tuple[str, ...] = (KB_A,),
    profile: str = PROFILE_SPEED,
) -> KeyPolicySnapshot:
    """只关心本票两列的一份配置快照；档位取 speed 是为了把链路钉成向量一路。"""

    return replace(
        DEV_KEY_POLICY,
        profile=profile,
        allowed_kb_ids=allowed_kb_ids,
        retrieve_qps=qps,
        retrieve_daily=daily,
    )


def _plan(*, qps: int = 50, daily: int = 1000, **overrides: Any) -> PlanSpec:
    return replace(PLAN_PRESETS[PLAN_PRO], retrieve_qps=qps, retrieve_daily=daily, **overrides)


class _Entry:
    """一次测试里的两条入口 + 一个可换的身份（换 Key 就是换 holder 上的上下文）。"""

    def __init__(
        self,
        client: TestClient,
        session: _WriteSession,
        holder: dict[str, KeyContext],
        store: _FakeCounterStore,
        enqueued: list[dict[str, Any]],
        async_session: Any = None,
    ):
        self.client = client
        self.session = session
        self.store = store
        self.enqueued = enqueued
        #: 上传那条路径的**异步**会话（票 #132）：被拒的上传同样不该在它上面留下行。
        self.async_session = async_session
        self._holder = holder
        self._mcp_headers: dict[str, str] = {}

    def use(
        self,
        *,
        key_id: str = KEY_A,
        policy: KeyPolicySnapshot | None = None,
        plan: PlanSpec | None = None,
    ) -> None:
        self._holder["context"] = KeyContext(
            tenant_id=TENANT_ID,
            key_id=key_id,
            policy=policy if policy is not None else _policy(),
            plan=plan if plan is not None else _plan(),
        )

    def retrieve(self, kb_ids: list[str] | None = None) -> dict:
        response = self.client.post(
            RETRIEVE_PATH, json={"kb_ids": kb_ids or [KB_A], "query": "退款多久到账"}
        )
        assert response.status_code == 200
        return response.json()

    def create_kb(self) -> dict:
        response = self.client.post(
            CREATE_KB_PATH,
            json={"name": "新库", "split_strategy": "size"},
        )
        assert response.status_code == 200
        return response.json()

    def upload(self, *, kb_id: str = KB_A) -> dict:
        response = self.client.post(
            UPLOAD_PATH,
            json={"kb_id": kb_id, "title": "手册", "content": "正文"},
        )
        assert response.status_code == 200
        return response.json()

    def initialize_mcp(self) -> None:
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t79", "version": "0"},
                },
            },
            headers=JSONRPC_HEADERS,
        )
        assert response.status_code == 200
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._mcp_headers["mcp-session-id"] = session_id
        self.client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**JSONRPC_HEADERS, **self._mcp_headers},
        )

    def mcp_retrieve(self, kb_ids: list[str] | None = None) -> dict:
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "rag_retrieve",
                    "arguments": {
                        "kb_ids": kb_ids or [KB_A],
                        "query": "退款多久到账",
                    },
                },
            },
            headers={**JSONRPC_HEADERS, **self._mcp_headers},
        )
        assert response.status_code == 200
        return response.json()["result"]


def _mcp_code(result: dict) -> int:
    """MCP 侧的业务码藏在「工具执行失败」的文本里：``<code>|<msg>``。"""

    if not result.get("isError"):
        return int(ErrorCode.SUCCESS)
    text = result["content"][0]["text"]
    match = re.search(r"(\d{5})\|", text)
    assert match, f"MCP 失败结果里没有错误码：{text!r}"
    return int(match.group(1))


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Entry]:
    """HTTP + MCP 挂同一个应用；身份由 holder 给出，计数器经注入点换成 fake。"""

    main = importlib.import_module("app.api.router")
    error_handlers = importlib.import_module("app.api.error_handlers")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    document_service = importlib.import_module("app.services.document_service")
    mcp_server = importlib.import_module("app.mcp_server")
    from app.api.deps import get_current_key

    monkeypatch.setattr(service, "get_embedding_provider", _FakeEmbedding)
    monkeypatch.setattr(service, "get_vector_store", _FakeVectorStore)

    def _visible_row(kb_id: str, tenant_id: str) -> Any:
        return SimpleNamespace(
            id=kb_id,
            tenant_id=tenant_id,
            name=f"库 {kb_id}",
            description=None,
            split_strategy="size",
            allowed_formats=["markdown"],
            settings={"chunk_size": 400},
        )

    async def _visible_kb(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return _visible_row(kb_id, tenant_id)

    # 可见性替身只有一个（票 #130 之后取库只有异步那一个入口）：检索链路、上传登记与
    # 重建都走 ``await get_by_id_for_tenant(...)``，所以替身必须是协程。
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _visible_kb)
    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(
        document_service, "enqueue_document", lambda **kwargs: enqueued.append(kwargs)
    )

    # 上传链路是异步栈（票 #132）：会话来自异步依赖，被拒的上传不该在它上面留下行。
    # 与写路径**同一个**假库（``override_all_db`` 的异步面是同一个对象的薄适配），
    # 于是「被拒的请求没落行」在两条栈上是同一句话，而不是两个各记一份的替身。
    session = _WriteSession()

    holder: dict[str, KeyContext] = {}
    holder["context"] = KeyContext(
        tenant_id=TENANT_ID, key_id=KEY_A, policy=_policy(), plan=_plan()
    )

    async def _identity(*_args: Any, **_kwargs: Any) -> KeyContext:
        """MCP 侧的身份替身是**协程**：判定内核自票 #130 起是 ``async``。"""

        return holder["context"]

    monkeypatch.setattr(mcp_server, "resolve_identity", _identity)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    store = _FakeCounterStore()
    redis_client.set_redis_client(store)

    # 两扇门挂到同一个应用：MCP 是子应用，判定内核与配额门禁都是同一份实现。
    mcp_app = mcp_server.create_mcp_http_app(
        # 检索链路用异步会话（第 11 步票 #127）：与 HTTP 侧同一个替身，包一层异步面。
        get_async_db=lambda: AsyncSessionAdapter(session),
        retrieve=service.retrieve,
        list_kbs=kb_repo.list_by_tenant,
    )
    app = FastAPI(lifespan=combine_lifespans(mcp_app.lifespan))
    app.include_router(main.api_router)
    error_handlers.install_handlers(app)
    app.mount("/mcp", mcp_app)
    # 四个会话入口一次换全（读 / 写 × 同步 / 异步）：写事务用生产实现，于是「一个请求
    # 一笔事务」这条纪律在离线套件里照旧被真实代码跑一遍。
    view = fake_db.override_all_db(app, session)
    app.dependency_overrides[get_current_key] = lambda: holder["context"]

    try:
        with TestClient(app) as client:
            result = _Entry(client, session, holder, store, enqueued, view)
            result.initialize_mcp()
            yield result
    finally:
        app.dependency_overrides.clear()
        redis_client.reset_redis_client()


# ───────────────────────────────────────────── 窗口与预置


def _window() -> QuotaWindow:
    """与生产同一个时钟：窗口口径不是被测逻辑，测试从同一个对象上取。"""

    return QuotaCounters(_FakeCounterStore()).window()


def _seconds_from(window: QuotaWindow, span: int = 3) -> list[QuotaWindow]:
    """当前秒与随后两秒：让「已计到上限」的预置不因跨秒边界而失效。"""

    base = int(window.qps)
    return [QuotaWindow(qps=str(base + offset), daily=window.daily) for offset in range(span)]


def _prefill_qps(store: _FakeCounterStore, *, level: str, ident: str, limit: int) -> QuotaWindow:
    """把该层级当前秒及随后两秒都预置成「已经计到上限」。"""

    window = _window()
    for item in _seconds_from(window):
        store.values[qps_key(level=level, ident=ident, window=item)] = limit
    return window


def _prefill_daily(store: _FakeCounterStore, *, level: str, ident: str, limit: int) -> QuotaWindow:
    window = _window()
    store.values[daily_key(level=level, ident=ident, window=window)] = limit
    return window


def _sum_of(store: _FakeCounterStore, prefix: str) -> int:
    """按前缀汇总：不断言具体的秒 / 日，避免跨秒边界把断言弄成间歇性失败。"""

    return sum(value for key, value in store.values.items() if key.startswith(prefix))


# ───────────────────────────────────────────── 检索：两个错误码


def test_a_qps_burst_is_rate_limited_with_20005(entry: _Entry) -> None:
    entry.use(policy=_policy(qps=1), plan=_plan(qps=50))
    _prefill_qps(entry.store, level="key", ident=KEY_A, limit=1)

    body = entry.retrieve()

    assert body["code"] == ErrorCode.API_RATE_LIMIT
    assert body["data"] is None


def test_an_exhausted_daily_allowance_is_a_quota_error_with_20014(entry: _Entry) -> None:
    entry.use(policy=_policy(qps=10, daily=1), plan=_plan(daily=1000))
    _prefill_daily(entry.store, level="key", ident=KEY_A, limit=1)

    body = entry.retrieve()

    assert body["code"] == ErrorCode.QUOTA_EXCEEDED
    assert body["data"] is None


def test_a_rejected_request_records_nothing(entry: _Entry) -> None:
    """被拒的请求不扣额度：否则一个跑飞的集成能在重试里把当天额度烧光。"""

    entry.use(policy=_policy(qps=1), plan=_plan(qps=50))
    _prefill_qps(entry.store, level="key", ident=KEY_A, limit=1)
    before = dict(entry.store.values)

    assert entry.retrieve()["code"] == ErrorCode.API_RATE_LIMIT

    assert entry.store.values == before, "拒绝不记账"


def test_rejection_happens_before_the_retrieval_burns_an_embedding(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拒绝先于执行：被限流的请求连一次嵌入都不该烧（否则额度与算力双输）。"""

    embedding_calls: list[str] = []

    class _RecordingEmbedding(_FakeEmbedding):
        async def embed_query(self, query: str) -> list[float]:
            embedding_calls.append(query)
            return await super().embed_query(query)

    service = importlib.import_module("app.services.rag_service")
    monkeypatch.setattr(service, "get_embedding_provider", _RecordingEmbedding)
    entry.use(policy=_policy(qps=1), plan=_plan(qps=50))
    _prefill_qps(entry.store, level="key", ident=KEY_A, limit=1)

    assert entry.retrieve()["code"] == ErrorCode.API_RATE_LIMIT

    assert embedding_calls == [], "拒绝先于召回"


def test_a_successful_retrieval_is_counted_at_both_levels_exactly_once(entry: _Entry) -> None:
    entry.use(policy=_policy(qps=10, daily=100), plan=_plan(qps=50, daily=1000))

    body = entry.retrieve()

    assert body["code"] == ErrorCode.SUCCESS
    store = entry.store
    assert _sum_of(store, f"quota:qps:key:{KEY_A}:") == 1
    assert _sum_of(store, f"quota:qps:tenant:{TENANT_ID}:") == 1
    assert _sum_of(store, f"quota:daily:key:{KEY_A}:") == 1
    assert _sum_of(store, f"quota:daily:tenant:{TENANT_ID}:") == 1


def test_one_key_running_hot_does_not_affect_another_key_of_the_same_tenant(
    entry: _Entry,
) -> None:
    """Key 级卡住的只是这一把：同租户另一把 Key 的额度必须完好。"""

    entry.use(key_id=KEY_A, policy=_policy(qps=1), plan=_plan(qps=50))
    assert entry.retrieve()["code"] == ErrorCode.SUCCESS

    _prefill_qps(entry.store, level="key", ident=KEY_A, limit=1)
    assert entry.retrieve()["code"] == ErrorCode.API_RATE_LIMIT

    entry.use(key_id=KEY_B, policy=_policy(qps=1), plan=_plan(qps=50))
    assert entry.retrieve()["code"] == ErrorCode.SUCCESS, "另一把 Key 不受连累"

    assert _sum_of(entry.store, f"quota:daily:key:{KEY_B}:") == 1


def test_the_tenant_aggregate_caps_the_sum_of_all_keys(entry: _Entry) -> None:
    """租户级是几把 Key 的加总：单看每把都没超，合起来必须撞在套餐上限上。"""

    entry.use(key_id=KEY_A, policy=_policy(daily=100), plan=_plan(daily=2))
    assert entry.retrieve()["code"] == ErrorCode.SUCCESS
    entry.use(key_id=KEY_B, policy=_policy(daily=100), plan=_plan(daily=2))
    assert entry.retrieve()["code"] == ErrorCode.SUCCESS

    entry.use(key_id=KEY_A, policy=_policy(daily=100), plan=_plan(daily=2))
    assert entry.retrieve()["code"] == ErrorCode.QUOTA_EXCEEDED


def test_a_plan_shrunk_mid_session_takes_effect_on_the_next_call(entry: _Entry) -> None:
    """改小日配额立刻生效：账本比的是**当前**额度，不是进门时记下的快照。"""

    entry.use(policy=_policy(daily=100), plan=_plan(daily=100))
    assert entry.retrieve()["code"] == ErrorCode.SUCCESS

    # 管理员把套餐日配额从 100 改成 1（下一次调用按新值判，不需要重启 / 等缓存过期）。
    entry.use(policy=_policy(daily=100), plan=_plan(daily=1))

    assert entry.retrieve()["code"] == ErrorCode.QUOTA_EXCEEDED


def test_one_retrieval_across_three_knowledge_bases_counts_daily_once(entry: _Entry) -> None:
    """跨多库只算一次：按库记账会逼调用方把一次检索拆成三次。"""

    entry.use(
        policy=_policy(allowed_kb_ids=(KB_A, KB_B, KB_C)),
        plan=_plan(),
    )

    body = entry.retrieve([KB_A, KB_B, KB_C])

    assert body["code"] == ErrorCode.SUCCESS
    store = entry.store
    assert _sum_of(store, f"quota:daily:key:{KEY_A}:") == 1
    assert _sum_of(store, f"quota:daily:tenant:{TENANT_ID}:") == 1


def test_the_switch_off_makes_the_gate_touch_no_redis_at_all(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关着 = no-op：一次 get / incr / expire 都不该发生（默认套件因此离线）。"""

    exploding = _ExplodingStore()
    redis_client.set_redis_client(exploding)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)

    body = entry.retrieve()

    assert body["code"] == ErrorCode.SUCCESS
    assert exploding.calls == [], "关掉时连一条 Redis 命令都不该发"


# ───────────────────────────────────────────── MCP：同一扇门禁，同一套码


def test_mcp_rejects_a_qps_burst_with_the_same_code(entry: _Entry) -> None:
    entry.use(policy=_policy(qps=1), plan=_plan(qps=50))
    _prefill_qps(entry.store, level="key", ident=KEY_A, limit=1)

    result = entry.mcp_retrieve()

    assert result["isError"] is True
    assert _mcp_code(result) == ErrorCode.API_RATE_LIMIT


def test_mcp_counts_a_successful_retrieval_at_both_levels(entry: _Entry) -> None:
    entry.use(policy=_policy(qps=10, daily=100), plan=_plan(qps=50, daily=1000))

    result = entry.mcp_retrieve()

    assert not result.get("isError"), result
    store = entry.store
    assert _sum_of(store, f"quota:daily:key:{KEY_A}:") == 1
    assert _sum_of(store, f"quota:daily:tenant:{TENANT_ID}:") == 1


# ───────────────────────────────────────────── 写路径：静态资源配额


def test_building_the_n_plus_first_knowledge_base_is_a_quota_error(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库数上限落在建库这条写路径上：第 N+1 个库当场 20014，且不落行。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    entry.use(plan=replace(PLAN_PRESETS[PLAN_FREE], max_kb=1))

    async def _one_kb(_db: Any, _tenant_id: str) -> int:
        return 1

    monkeypatch.setattr(kb_repo, "count_by_tenant", _one_kb)

    body = entry.create_kb()

    assert body["code"] == ErrorCode.QUOTA_EXCEEDED
    assert entry.session.added == [], "被拒的请求不落行"


def test_the_last_knowledge_base_under_the_limit_is_allowed(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """额度 N 表示「最多 N 个」：已有 N-1 个时建第 N 个必须成功。"""

    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    entry.use(plan=replace(PLAN_PRESETS[PLAN_FREE], max_kb=1))

    async def _no_kb(_db: Any, _tenant_id: str) -> int:
        return 0

    monkeypatch.setattr(kb_repo, "count_by_tenant", _no_kb)

    body = entry.create_kb()

    assert body["code"] == ErrorCode.SUCCESS
    assert len(entry.session.added) == 1


def test_uploading_over_documents_per_kb_is_a_quota_error(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单库文档数：库满了就不再落行、不再入队。"""

    doc_repo = importlib.import_module("app.repositories.document_repository")
    entry.use(plan=replace(PLAN_PRESETS[PLAN_FREE], max_documents_per_kb=30))

    async def _count_docs(_db, _kb_id, *, tenant_id: str) -> int:
        return 30

    async def _count_processing(_db, *, tenant_id: str) -> int:
        return 0

    monkeypatch.setattr(doc_repo, "count_by_kb", _count_docs)
    monkeypatch.setattr(doc_repo, "count_processing_for_tenant", _count_processing)

    body = entry.upload()

    assert body["code"] == ErrorCode.QUOTA_EXCEEDED
    assert entry.async_session.added == [], "被拒的上传不该在异步会话上留下行"
    assert entry.enqueued == [], "被拒的上传不该往队列里塞任务"


def test_uploading_over_the_concurrent_processing_allowance_is_a_quota_error(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发处理数是「别把其他租户的 Worker 饿死」的那一条，也在上传路径上。"""

    doc_repo = importlib.import_module("app.repositories.document_repository")
    entry.use(plan=replace(PLAN_PRESETS[PLAN_FREE], max_processing_documents=1))

    async def _count_docs(_db, _kb_id, *, tenant_id: str) -> int:
        return 0

    async def _count_processing(_db, *, tenant_id: str) -> int:
        return 1

    monkeypatch.setattr(doc_repo, "count_by_kb", _count_docs)
    monkeypatch.setattr(doc_repo, "count_processing_for_tenant", _count_processing)

    body = entry.upload()

    assert body["code"] == ErrorCode.QUOTA_EXCEEDED
    assert entry.enqueued == []


def test_reindex_charges_the_concurrent_processing_allowance(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重建跳过「单库文档数」（文档本来就在），却照样入队——不能漏掉并发这一条。"""

    doc_repo = importlib.import_module("app.repositories.document_repository")
    entry.use(plan=replace(PLAN_PRESETS[PLAN_FREE], max_processing_documents=1))

    async def _failed_doc(_db: Any, document_id: str, *, tenant_id: str) -> Any:
        return SimpleNamespace(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=KB_A,
            title="手册",
            status=DocumentStatus.FAILED,
            index_claimed_at=None,
            error_message="上次失败",
            source_type="markdown",
            source_file_path=None,
        )

    async def _one_processing(_db: Any, *, tenant_id: str) -> int:
        return 1

    monkeypatch.setattr(doc_repo, "get_by_id_for_tenant_async", _failed_doc)
    monkeypatch.setattr(doc_repo, "count_processing_for_tenant", _one_processing)

    response = entry.client.post("/api/v1/documents/doc-1/reindex")
    assert response.status_code == 200

    body = response.json()
    assert body["code"] == ErrorCode.QUOTA_EXCEEDED
    assert entry.enqueued == [], "被拒的重建不该入队"
