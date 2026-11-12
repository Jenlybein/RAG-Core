"""按事件循环的 HTTP 客户端注册表：同一 loop 复用、不同 loop 隔离、确定性收尾。

**为什么按 loop 隔离是硬约束而不是洁癖**：``httpx.AsyncClient`` 的连接池绑在创建它的
事件循环上（``elasticsearch`` 的 Provider 已经为这条付过一次代价——Celery 每个任务
``asyncio.run`` 一次，跨 loop 复用同一个客户端的下场是第二个任务立刻
``RuntimeError: Event loop is closed``）。注册表因此按「当前正在运行的 loop」发客户端，
不同 loop 各拿各的。

本文件守的契约：

* 同一 loop 里取两次 = 同一个实例（复用的意义就在这里）；
* 用两次 ``asyncio.run`` 起两个 loop = 两个不同实例（隔离）；
* 没有运行中的 loop 时取客户端要**立刻报错**，不许拿一个「将来会绑到某个 loop 上」的
  客户端糊过去；
* 收尾是确定性的：``aclose_current_loop_clients()`` 关掉当前 loop 的那一个并把它从
  注册表摘掉；``aclose_all_clients()`` 尽力逐个 loop 关，关不掉的只记 warning 不抛；
* ``reset_http_clients()`` 只清注册表（测试用），不联网、不尝试关闭。

「注册表里造两个假客户端」的做法是**直接用注册表本身**（``_CLIENTS``）：不去为测试顺手
加一个生产用不上的公开注入口，测试要造的状态就是这张表本身。
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
    """只要被关掉就记一笔的替身：注册表对客户端的要求只有 ``aclose()``。"""

    def __init__(self) -> None:
        self.is_closed = False
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.is_closed = True


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """每个用例前后清空注册表：注册表是进程级状态，不许在用例之间串味。"""

    http_clients.reset_http_clients()
    yield
    http_clients.reset_http_clients()


# ───────────────────────────────────────────────── 同一 loop 复用 / 不同 loop 隔离


def test_two_lookups_on_the_same_loop_return_the_same_client() -> None:
    """同一 loop 里取两次必须是同一个对象——否则「复用」只是字面上的。"""

    async def _run() -> None:
        first = http_clients.get_http_client()
        second = http_clients.get_http_client()
        try:
            assert first is second
            assert isinstance(first, httpx.AsyncClient)
        finally:
            await http_clients.aclose_current_loop_clients()

    asyncio.run(_run())


def test_two_loops_get_two_different_clients() -> None:
    """两个 ``asyncio.run`` 就是两个 loop：客户端必须各是各的（既有事故的约束）。

    列表里存**对象**而不是 ``id()``：前一个 loop 关闭后它的客户端可能已经被回收，``id``
    会被下一个对象复用——那样这条用例会随机变绿，而它守的恰好是「绝不能复用」。
    """

    seen: list[httpx.AsyncClient] = []

    async def _run() -> None:
        seen.append(http_clients.get_http_client())
        await http_clients.aclose_current_loop_clients()

    asyncio.run(_run())
    asyncio.run(_run())

    assert len(seen) == 2
    assert seen[0] is not seen[1], "跨 loop 复用同一个 AsyncClient 就是那次事故的复现"


def test_asking_for_a_client_without_a_running_loop_fails_loudly() -> None:
    """没有运行中的 loop = 没有可复用的连接池：当场报错，不返回一个「无主」客户端。"""

    with pytest.raises(RuntimeError):
        http_clients.get_http_client()


def test_the_model_traffic_client_carries_the_configured_limits() -> None:
    """连接池参数是这一票的规格：100 / 20 / 30s，写在一处、可被断言。"""

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


def test_closing_the_current_loop_removes_its_client_from_the_registry() -> None:
    """关掉之后注册表里不再有它：下一次取到的是**新**实例（旧池已经关了）。"""

    async def _run() -> None:
        first = http_clients.get_http_client()
        await http_clients.aclose_current_loop_clients()
        assert first.is_closed is True

        second = http_clients.get_http_client()
        try:
            assert second is not first
        finally:
            await http_clients.aclose_current_loop_clients()

    asyncio.run(_run())


def test_closing_the_current_loop_is_idempotent() -> None:
    """收尾可能被调用两次（lifespan 退出 + 进程收尾）：第二次必须是安静的 no-op。"""

    async def _run() -> None:
        http_clients.get_http_client()
        await http_clients.aclose_current_loop_clients()
        await http_clients.aclose_current_loop_clients()

    asyncio.run(_run())


def test_closing_all_clients_closes_every_registered_loop() -> None:
    """``aclose_all_clients()`` 是进程收尾：注册表里每个 loop 名下的客户端都要关掉。"""

    loops = [asyncio.new_event_loop(), asyncio.new_event_loop()]
    fakes = [_FakeClient(), _FakeClient()]
    for loop, fake in zip(loops, fakes, strict=True):
        http_clients._CLIENTS[loop] = fake  # type: ignore[assignment]

    try:
        asyncio.run(http_clients.aclose_all_clients())
    finally:
        for loop in loops:
            loop.close()

    assert [fake.is_closed for fake in fakes] == [True, True]
    assert [fake.aclose_calls for fake in fakes] == [1, 1]


def test_closing_all_clients_also_closes_the_process_wide_sync_client() -> None:
    """进程收尾要把同步客户端一起关掉：它同样占着连接池。"""

    sync_client = http_clients.get_sync_http_client()

    asyncio.run(http_clients.aclose_all_clients())

    assert sync_client.is_closed is True


def test_closing_all_clients_never_raises_for_a_loop_it_cannot_close() -> None:
    """关不掉的（loop 已经关了）只记 warning，不抛：进程收尾不该被一条噪音打断。"""

    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    fake = _FakeClient()
    http_clients._CLIENTS[dead_loop] = fake  # type: ignore[assignment]

    asyncio.run(http_clients.aclose_all_clients())  # 不抛就是通过

    assert fake.is_closed is False
    assert fake.aclose_calls == 0


def test_reset_only_clears_the_registry() -> None:
    """``reset_http_clients()`` 是测试接缝：清表，**不**联网、不去关任何东西。"""

    loop = asyncio.new_event_loop()
    fake = _FakeClient()
    http_clients._CLIENTS[loop] = fake  # type: ignore[assignment]
    try:
        http_clients.reset_http_clients()
        assert fake.aclose_calls == 0
        assert fake.is_closed is False
    finally:
        loop.close()


# ─────────────────────────────────────────────── 应用与 Worker 的确定性收尾


def test_the_app_lifespan_closes_the_loop_clients_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用关闭后，注册表里属于主循环的客户端已经关了。

    接缝是两处真实存在的动作：注册表的构造点（换成替身，记录谁被建、谁被关）与
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
    # lifespan 在**它自己的** loop 里跑；启动期这个同步回调因此能取到该 loop 的客户端。
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


def test_the_worker_task_keeps_the_clients_it_built_across_tasks(
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
    assert len(http_clients._CLIENTS) == 1, "注册表里的条目属于那个仍然活着的常驻循环"

    # 第二个任务：同一个客户端，仍然没被关——这就是「连接复用」在客户端侧的样子。
    tasks._run_indexing(_fake_task(), "doc-2", tenant_id="t", kb_id="kb-1")
    assert len(created) == 1, "第二个任务不该再建一个客户端"
    assert created[0].is_closed is False

    shutdown_worker_loop()
    assert created[0].is_closed is True, "确定性释放发生在 Worker 退出，而不是任务结束"
    assert len(http_clients._CLIENTS) == 0
    assert get_worker_loop().alive is False


def test_the_worker_task_leaves_the_clients_open_even_when_indexing_fails(
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
