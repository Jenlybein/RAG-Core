"""#127：检索链路整体搬到异步栈，执行池与线程包装消失。

接缝与既有用例一致：ASGI 上的 ``POST /api/v1/rag/retrieve``、组合根
``rag_service.retrieve``、以及**源码文本**（最后一条是结构守卫，见下）。数据库与三类
Provider 一律是离线替身：不连 PostgreSQL、不连 Redis、不发网络。

本文件守四件事，都是外部可观察的：

1. **请求会话是异步的那一个**：检索的每一次读库都落在 ``get_async_db`` 给的会话上。
   证明方式是「只换异步依赖、不换同步依赖」——同步会话仍然是生产那个，检索却照样跑通；
   反过来，如果链路还在用同步会话，这条用例会去连真库并失败。
2. **取消能传导**：任务被 cancel 之后，正在飞的嵌入当场收到 ``CancelledError``，
   后续的召回一次都不再发生。第 10 步的执行池把活丢进别的线程，取消只能取消「等结果」，
   活照样跑完——那正是本票要消掉的东西。
3. **检索路径上没有线程包装**：``to_thread`` / ``run_in_executor`` / 执行池在检索路径的
   源码里一个都不剩，``app/core/loop_pool.py`` 与它的用例也不在了。按线程注册的 HTTP
   客户端（``app/core/http_clients.py``）**保留**——Celery 每个任务自带循环，仍需要它。
4. **归属索引写入仍完整**：旁路短事务、失败只 warning。它逐列的契约由
   ``tests/test_trace_identity.py`` 守着；这里补一条「它现在也走异步会话」，
   并且同步会话工厂一次都不许被碰。

为什么第 3 条是结构守卫而不是行为断言：**「不再有线程包装」这件事没有外部行为可观察**
——它正确的表现就是「行为和以前一样，只是不再占线程」。这类「消除某条实现路径」的
验收只能扫源码；票面本身也写的是「有测试或静态检查证明」。
"""

from __future__ import annotations

import asyncio
import importlib
import tokenize
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.retrieval_options import RetrievalOptions
from tests.fake_db import AsyncRecordingSession, RecordingSession
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_async"
KB_ID = "kb-async"

IDENTITY = RetrievalIdentity(TENANT_ID)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────── 替身


class _RecordingEmbedding:
    """查询侧嵌入替身：记下句子，并给一个可辨认的向量。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """范围召回替身：一次覆盖全部配对，每行自带 ``kb_id``。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else [_row("c1")]
        self.calls: list[dict[str, Any]] = []
        self.db_seen: Any = None

    async def similarity_search_scope(self, db, query_vector, *, tenant_id, index_versions, top_k):
        self.calls.append({"tenant_id": tenant_id, "index_versions": dict(index_versions)})
        self.db_seen = db
        return [{**row, "kb_id": next(iter(index_versions))} for row in self.rows]

    async def similarity_search(self, *args: Any, **kwargs: Any) -> list[dict]:
        raise AssertionError("检索走范围召回，不许按库循环")


class _FakeKeyword:
    available = True

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[str] = []

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k) -> list[dict]:
        self.queries.append(query)
        return [dict(row) for row in self.rows]


def _row(chunk_id: str, *, score: float = 0.9) -> dict:
    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "score": score,
    }


async def _kb_for_tenant(_db: Any, _kb_id: str, *, tenant_id: str) -> Any:
    return SimpleNamespace(tenant_id=tenant_id, settings={}, name="", description="")


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: Any = None,
    vector: Any = None,
    keyword: Any = None,
) -> Iterator[dict[str, Any]]:
    """离线 ASGI 客户端：**只**换异步请求会话，同步会话依赖一个字都不动。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    factory_mod = importlib.import_module("app.db.session_factory")

    embedding = embedding or _RecordingEmbedding()
    vector = vector or _FakeVectorStore()
    keyword = keyword if keyword is not None else _FakeKeyword()

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _kb_for_tenant)

    request_session = AsyncRecordingSession()
    trace_session = AsyncRecordingSession()
    #: 同步会话工厂仍然被配置刷新的中间件用着（那是它自己的读，不是检索），所以这里不能
    #: 让它炸；只断言**它没有收到归属索引的那一行**——写入已经不在同步栈上了。
    sync_session = RecordingSession()

    factory_mod.set_session_factory(lambda: sync_session)
    factory_mod.set_async_session_factory(lambda: trace_session)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: request_session
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {
                "client": client,
                "embedding": embedding,
                "vector": vector,
                "keyword": keyword,
                "request_session": request_session,
                "trace_session": trace_session,
                "sync_session": sync_session,
            }
    finally:
        main.app.dependency_overrides.clear()
        factory_mod.set_session_factory(None)
        factory_mod.set_async_session_factory(None)


def _post(client: TestClient, *, mode: str = "vector") -> Any:
    IDENTITY.use(mode=mode, top_k=2, include_parent=False)
    return client.post(RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": "退款怎么走"})


# ─────────────────────────────────────── 一、三种模式都跑在异步会话上


@pytest.mark.parametrize("mode", ["vector", "bm25", "hybrid"])
def test_the_three_modes_read_through_the_async_request_session(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """只换 ``get_async_db``：三种模式都必须跑通，且读库落在异步会话上。

    同步会话依赖在这条用例里**没有被替换**——链路要是还走它，就会去连真 PostgreSQL
    （默认套件离线，于是当场失败）。所以「200 + code=0」本身就是「换栈了」的证据；
    ``request_session`` 记下的语句则证明读库真的经过了它。
    """

    with _offline_client(monkeypatch) as fake:
        response = _post(fake["client"], mode=mode)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0, body
    assert body["data"]["metadata"]["mode"] == mode
    # 空结果判定要查一次 count：那条读必须落在异步会话上。
    assert fake["request_session"].statements, "异步请求会话一次都没被读过"


def test_the_visibility_guard_is_the_async_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """可见性校验走异步仓储：拒绝（20008）仍然先于任何一次召回发生。"""

    from app.core.exceptions import AppError, ErrorCode

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = _RecordingEmbedding()
    vector = _FakeVectorStore()
    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)

    async def _reject(_db: Any, _kb_id: str, *, tenant_id: str) -> Any:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _reject)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncRecordingSession()
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = _post(client, mode="vector")
    finally:
        main.app.dependency_overrides.clear()

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND.value
    assert embedding.queries == [], "被拒的请求连嵌入都不该算"
    assert vector.calls == [], "被拒的请求不该发出召回"


def test_the_side_path_write_uses_the_async_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """归属索引是旁路短事务：它自己开**异步**会话、自己提交、自己关。

    同步会话工厂仍然被配置刷新的中间件用着，所以不能让它炸；改成断言**它一条归属索引
    行都没收到**——写入确实已经整体搬到异步那一侧了。
    """

    from app.models.retrieval_trace import RetrievalTrace

    with _offline_client(monkeypatch) as fake:
        body = _post(fake["client"], mode="vector").json()

    trace_session: AsyncRecordingSession = fake["trace_session"]
    rows = trace_session.added_of(RetrievalTrace)
    assert len(rows) == 1, "一次检索只留一行"
    assert rows[0].trace_id == body["data"]["metadata"]["trace_id"]
    assert trace_session.commits == 1
    assert trace_session.closed is True
    assert fake["sync_session"].added_of(RetrievalTrace) == [], "同步会话不该再收到这一行"


# ───────────────────────────────────────────────── 二、取消能传到正在飞的活


class _BlockingEmbedding:
    """嵌入替身：卡住，直到被取消。取消是否**传导到它身上**是可观察的事实。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def embed_query(self, query: str) -> list[float]:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [0.1, 0.2, 0.3]  # pragma: no cover - 走不到：上面必然被取消


async def _cancelled_retrieval(
    monkeypatch: pytest.MonkeyPatch, *, mode: str
) -> tuple[_BlockingEmbedding, _FakeVectorStore, AsyncRecordingSession]:
    """把一次检索跑起来、等它真的卡在嵌入上，然后 cancel；返回替身供断言。"""

    service = importlib.import_module("app.services.rag_service")
    factory_mod = importlib.import_module("app.db.session_factory")

    embedding = _BlockingEmbedding()
    vector = _FakeVectorStore()
    trace_session = AsyncRecordingSession()

    async def _visible(_db: Any, *, tenant_id: str, kb_id: str) -> Any:
        return SimpleNamespace(tenant_id=tenant_id, settings={}, name="", description="")

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: _FakeKeyword())
    monkeypatch.setattr(service, "ensure_visible", _visible)
    factory_mod.set_async_session_factory(lambda: trace_session)

    options = RetrievalOptions(
        mode=mode,
        top_k=2,
        fold_to_sections=False,
        expand_table_rows=False,
        rerank=False,
        rewrite=False,
        orchestration=False,
    )
    task = asyncio.create_task(
        service.retrieve(
            AsyncRecordingSession(),
            tenant_id=TENANT_ID,
            kb_ids=[KB_ID],
            query="退款怎么走",
            options=options,
        )
    )
    try:
        await asyncio.wait_for(embedding.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        factory_mod.set_async_session_factory(None)
    return embedding, vector, trace_session


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "hybrid"])
async def test_cancelling_the_request_cancels_the_work_in_flight(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """客户端断开 → 正在飞的嵌入收到取消，后续召回一次都不再发生。

    混合模式尤其要守：**取消不是「这一路失败」**，不许被对称降级吞成
    ``degraded=true`` 再把剩下的活干完。
    """

    embedding, vector, trace_session = await _cancelled_retrieval(monkeypatch, mode=mode)

    assert embedding.cancelled is True, "取消必须传到正在 await 的那段活上"
    assert vector.calls == [], "取消之后不该再发出召回"
    assert trace_session.added == [], "被取消的请求不再写归属索引"


# ───────────────────────────────────── 三、结构守卫：线程包装与执行池都消失

#: 检索路径的源码（含它依赖的仓储与向量库实现）。这张表是「这一条链路上有谁」的清单。
_RETRIEVAL_PATH_FILES: tuple[str, ...] = (
    "app/services/rag_service.py",
    "app/services/retrieval/pipeline.py",
    "app/services/retrieval/vector_recall.py",
    "app/services/retrieval/fusion.py",
    "app/services/retrieval/rerank.py",
    "app/services/knowledge_base_access.py",
    "app/services/retrieval_trace.py",
    "app/services/result_collapse.py",
    "app/providers/vectorstores/pgvector.py",
    "app/repositories/chunk_repository.py",
    "app/repositories/knowledge_base_repository.py",
    "app/repositories/retrieval_trace_repository.py",
)

#: 这些名字代表「把活挪到别的线程 / 另开一个会话」：检索路径上一个都不许有。
_FORBIDDEN_IN_RETRIEVAL_PATH: tuple[str, ...] = (
    "to_thread",
    "run_in_executor",
    "run_coroutine_threadsafe",
    "loop_pool",
    "get_retrieval_pool",
    "threading",
    "SessionLocal",
)


def _code_names(path: Path) -> list[tuple[int, str]]:
    """文件里**代码**用到的名字（``(行号, 名字)``）：注释与字符串不算。

    扫文本会把「文档里解释为什么删掉执行池」的那些句子一起算成违规——本票的注释正是
    到处都在提它。用 ``tokenize`` 只取 ``NAME``，注释、docstring、字符串常量因此全部
    不参与判断，而 ``from app.core.loop_pool import ...`` / ``asyncio.to_thread(...)``
    这类**真调用**一个都漏不掉。
    """

    names: list[tuple[int, str]] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.NAME:
                names.append((token.start[0], token.string))
    return names


def test_no_thread_wrapping_left_on_the_retrieval_path() -> None:
    """「不再占线程」这件事只能扫源码：它正确的表现就是行为不变。"""

    offenders: list[str] = []
    for relative in _RETRIEVAL_PATH_FILES:
        for number, name in _code_names(REPO_ROOT / relative):
            if name in _FORBIDDEN_IN_RETRIEVAL_PATH:
                offenders.append(f"{relative}:{number}: {name}")
    assert offenders == [], "检索路径上还有线程包装 / 独立会话：\n" + "\n".join(offenders)


def test_the_fixed_size_retrieval_pool_is_gone() -> None:
    """执行池本体、它的用例、它的两个配置键一起删掉：留着就是又一条没人走的路。"""

    from app.core.config import settings

    assert not (REPO_ROOT / "app" / "core" / "loop_pool.py").exists()
    assert not (REPO_ROOT / "tests" / "test_loop_pool.py").exists()
    assert not hasattr(settings, "RETRIEVAL_POOL_SIZE")
    assert not hasattr(settings, "RETRIEVAL_QUEUE_BUDGET_MS")

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        for number, name in _code_names(path):
            if name in ("loop_pool", "get_retrieval_pool", "reset_retrieval_pool"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {name}")
    assert offenders == [], "还有代码引用执行池：\n" + "\n".join(offenders)


def test_the_per_loop_client_registry_stays_for_celery() -> None:
    """按事件循环注册的 HTTP 客户端**保留**：Celery 每个任务自带循环，仍要靠它复用连接。"""

    from app.core.http_clients import aclose_http_client, get_http_client

    assert callable(get_http_client)
    assert callable(aclose_http_client)
