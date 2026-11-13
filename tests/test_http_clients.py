"""进程级 HTTP 客户端：一个进程一份连接池，收尾是确定性的。

**为什么从「按 loop 注册表」缩成「一个槽」**（第 11 步票 #134，ADR-0024）：注册表存在的
唯一理由是同一个进程里同时跑着好几个事件循环——第 10 步的检索执行池有 4 个 worker 循环，
Celery 又每个任务 ``asyncio.run`` 一次。第 11 步把这两件事都消掉了：API 进程只有主循环，
Worker 进程只有一个常驻循环（ADR-0023），跨进程本来就不共享内存。于是「按 loop 找客户端」
退化成「拿这一个」，弱引用表、逐 loop 关闭、往别的 loop 投协程那三段复杂度一起删掉。

**仍然守着的那条硬约束**：``httpx.AsyncClient`` 的连接池绑在创建它的循环上，跨循环复用
的下场是 ``RuntimeError: Event loop is closed``（ES Provider 付过一次代价）。所以槽里存着
「它是哪个循环的」，循环一变就重建——生产里永远不会变，只有测试会（一个用例一个循环）。

本文件守的契约：

* 同一循环里取两次 = 同一个实例；
* 循环换了 = 新实例（**绝不**跨循环复用）；
* 没有运行中的循环时取客户端要**立刻报错**；
* ``aclose_http_client()`` 关掉异步那一个与进程级同步那一个，幂等，没有就是安静 no-op；
* Worker 任务结束**不关**客户端（归常驻循环），``shutdown_worker_loop()`` 才关；
* ``reset_http_clients()`` 只丢引用（测试用），不联网、不尝试关闭。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import http_clients


class _FakeClient:
    """只要被关掉就记一笔的替身：模块对客户端的要求只有 ``aclose()`` 与 ``is_closed``。"""

    def __init__(self) -> None:
        self.is_closed = False
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.is_closed = True


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """每个用例前后丢掉那一槽：它是进程级状态，不许在用例之间串味。"""

    http_clients.reset_http_clients()
    yield
    http_clients.reset_http_clients()


# ───────────────────────────────────────────────── 同一循环复用 / 跨循环重建


def test_two_lookups_on_the_same_loop_return_the_same_client() -> None:
    """同一循环里取两次必须是同一个对象——否则「复用」只是字面上的。"""

    async def _run() -> None:
        first = http_clients.get_http_client()
        second = http_clients.get_http_client()
        try:
            assert first is second
            assert isinstance(first, httpx.AsyncClient)
        finally:
            await http_clients.aclose_http_client()

    asyncio.run(_run())


def test_a_different_loop_never_reuses_the_previous_client() -> None:
    """两个 ``asyncio.run`` 就是两个循环：客户端必须重建（既有事故的约束）。

    列表里存**对象**而不是 ``id()``：前一个循环关闭后它的客户端可能已经被回收，``id``
    会被下一个对象复用——那样这条用例会随机变绿，而它守的恰好是「绝不能复用」。
    """

    seen: list[httpx.AsyncClient] = []

    async def _run() -> None:
        seen.append(http_clients.get_http_client())
        await http_clients.aclose_http_client()

    asyncio.run(_run())
    asyncio.run(_run())

    assert len(seen) == 2
    assert seen[0] is not seen[1], "跨循环复用同一个 AsyncClient 就是那次事故的复现"


def test_a_client_left_over_from_a_dead_loop_is_rebuilt_not_reused() -> None:
    """上一个循环没关就退出（脚本、异常退出）时，下一个循环拿到的是**新**客户端。"""

    async def _run() -> httpx.AsyncClient:
        return http_clients.get_http_client()

    leaked = asyncio.run(_run())

    async def _again() -> httpx.AsyncClient:
        return http_clients.get_http_client()

    fresh = asyncio.run(_again())

    assert fresh is not leaked
    assert fresh.is_closed is False


def test_asking_for_a_client_without_a_running_loop_fails_loudly() -> None:
    """没有运行中的循环 = 没有可复用的连接池：当场报错，不返回一个「无主」客户端。"""

    with pytest.raises(RuntimeError):
        http_clients.get_http_client()


def test_the_model_traffic_client_carries_the_configured_limits() -> None:
    """连接池参数是第 10 步的规格：100 / 20 / 30s，写在一处、可被断言。"""

    limits = http_clients.MODEL_TRAFFIC_LIMITS

    assert limits.max_connections == 100
    assert limits.max_keepalive_connections == 20
    assert limits.keepalive_expiry == 30.0


def test_the_sync_client_is_process_wide() -> None:
    """同步客户端不绑事件循环，进程一个就够（它只是为了让框架的包装对象别另建一个）。"""

    first = http_clients.get_sync_http_client()
    second = http_clients.get_sync_http_client()

    assert first is second
    assert isinstance(first, httpx.Client)


# ─────────────────────────────────────────────────────────────── 确定性收尾


def test_closing_leaves_no_client_behind_so_the_next_lookup_builds_a_new_one() -> None:
    """关掉之后槽是空的：下一次取到的是**新**实例（旧池已经关了）。"""

    async def _run() -> None:
        first = http_clients.get_http_client()
        await http_clients.aclose_http_client()
        assert first.is_closed is True

        second = http_clients.get_http_client()
        try:
            assert second is not first
        finally:
            await http_clients.aclose_http_client()

    asyncio.run(_run())


def test_closing_is_idempotent() -> None:
    """收尾可能被调用两次（lifespan 退出 + Worker 退出）：第二次必须是安静的 no-op。"""

    async def _run() -> None:
        http_clients.get_http_client()
        await http_clients.aclose_http_client()
        await http_clients.aclose_http_client()

    asyncio.run(_run())


def test_closing_without_ever_building_a_client_is_a_quiet_noop() -> None:
    asyncio.run(http_clients.aclose_http_client())


def test_closing_also_closes_the_process_wide_sync_client() -> None:
    """收尾要把同步客户端一起关掉：它同样占着连接池。"""

    async def _run() -> None:
        http_clients.get_http_client()
        await http_clients.aclose_http_client()

    sync_client = http_clients.get_sync_http_client()

    asyncio.run(_run())

    assert sync_client.is_closed is True


def test_reset_only_drops_the_reference() -> None:
    """``reset_http_clients()`` 是测试接缝：丢引用，**不**联网、不去关任何东西。"""

    fake = _FakeClient()

    async def _run() -> None:
        http_clients._CLIENT = fake  # type: ignore[assignment]
        http_clients._CLIENT_LOOP = asyncio.get_running_loop()

    asyncio.run(_run())
    http_clients.reset_http_clients()

    assert fake.aclose_calls == 0
    assert fake.is_closed is False


# ─────────────────────────────────────────────── 应用与 Worker 的确定性收尾


def test_the_app_lifespan_closes_the_client_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用关闭后，运行期建过的那一个客户端已经关了。

    接缝是两处真实存在的动作：客户端的构造点（换成替身，记录谁被建、谁被关）与
    lifespan 启动时**必然**会调的那一步（``tokenizer.verify_ready``），让它顺手要一个
    客户端——于是「应用运行期间建过一个连接池」成为这个用例自己的事实。
    """

    import importlib

    created: list[_FakeClient] = []

    def _builder() -> _FakeClient:
        client = _FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr(http_clients, "_build_async_client", _builder)

    main = importlib.import_module("app.main")
    # lifespan 在**它自己的**循环里跑；启动期这个同步回调因此能取到该循环的客户端。
    monkeypatch.setattr(main.tokenizer, "verify_ready", lambda: http_clients.get_http_client())

    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert len(created) == 1, "应用运行期间确实建了一个客户端"
        assert created[0].is_closed is False

    assert created[0].is_closed is True, "lifespan 退出时必须把它关掉"
    assert created[0].aclose_calls == 1, "关一次就够（幂等的收尾）"


def _fake_task() -> Any:
    """任务体的轻量 ``self``：只需要 ``request.retries`` / ``max_retries``。"""

    from types import SimpleNamespace

    return SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=0, retry=None)


class _TaskSession:
    """任务体用到的最小**异步**会话替身（不碰数据库）。"""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}

    async def rollback(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def flush(self) -> None:
        return None


def _patch_task_runtime(monkeypatch: pytest.MonkeyPatch, service: Any) -> None:
    from app.tasks import indexing as tasks

    monkeypatch.setattr(tasks, "async_session_factory", lambda: _TaskSession)
    monkeypatch.setattr(tasks, "IndexingService", lambda: service)
    monkeypatch.setattr(tasks.runtime_settings, "ensure_runtime_overrides", lambda: None)

    async def _mark_failed(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tasks, "_mark_failed", _mark_failed)


def test_the_worker_task_keeps_the_client_it_built_across_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker 一个进程一个常驻循环：任务建的客户端**不归任务关**，跨任务复用。

    改造前每个任务 ``asyncio.run`` 一次，收尾必须写在协程的 ``finally`` 里（否则那个循环
    一关，客户端就再也没人关得掉）。ADR-0023 选了常驻循环之后，那句 ``finally`` 的含义
    反过来了：它等于**每个任务都把共享客户端关掉**，复用立刻归零。收尾上移到
    ``WorkerLoop.shutdown``（``worker_process_shutdown`` 触发）。
    """

    from app.db.worker_loop import get_worker_loop, shutdown_worker_loop
    from app.tasks import indexing as tasks

    created: list[_FakeClient] = []

    def _builder() -> _FakeClient:
        client = _FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr(http_clients, "_build_async_client", _builder)

    class _Service:
        async def index_existing_document(self, _db: Any, _document_id: str, *, reparse: bool):
            http_clients.get_http_client()  # 任务里真的会建客户端（嵌入要发请求）
            return None

    _patch_task_runtime(monkeypatch, _Service())

    tasks._run_indexing(_fake_task(), "doc-1", tenant_id="t", kb_id="kb-1")
    assert len(created) == 1
    assert created[0].is_closed is False, "任务结束时不许关共享客户端：下一个任务还要用它"
    assert http_clients._CLIENT is created[0], "那一槽属于仍然活着的常驻循环"

    # 第二个任务：同一个客户端，仍然没被关——这就是「连接复用」在客户端侧的样子。
    tasks._run_indexing(_fake_task(), "doc-2", tenant_id="t", kb_id="kb-1")
    assert len(created) == 1, "第二个任务不该再建一个客户端"
    assert created[0].is_closed is False

    shutdown_worker_loop()
    assert created[0].is_closed is True, "确定性释放发生在 Worker 退出，而不是任务结束"
    assert http_clients._CLIENT is None
    assert get_worker_loop().alive is False


def test_the_worker_task_leaves_the_client_open_even_when_indexing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败路径同理：客户端属于循环，不属于「这一条跑挂了的任务」。"""

    from app.db.worker_loop import shutdown_worker_loop
    from app.tasks import indexing as tasks

    created: list[_FakeClient] = []

    def _builder() -> _FakeClient:
        client = _FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr(http_clients, "_build_async_client", _builder)

    class _FailingService:
        async def index_existing_document(self, _db: Any, _document_id: str, *, reparse: bool):
            http_clients.get_http_client()
            raise RuntimeError("upstream exploded")

    _patch_task_runtime(monkeypatch, _FailingService())

    tasks._run_indexing(_fake_task(), "doc-1", tenant_id="t", kb_id="kb-1")

    assert created[0].is_closed is False
    shutdown_worker_loop()
    assert created[0].is_closed is True
