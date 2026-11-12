"""票 #126：最小 ASGI 探针——异步依赖真的在跑，且新旧两栈返回同一份响应。

探针**只挂在测试里**：一个临时 ``FastAPI`` 应用上两条并排的路由，一条走 ``get_db``
（同步），一条走 ``get_async_db``（异步），对**同一个查询**返回同一个响应体。不新增生产
端点，也不改任何现有路由——改路由是 #127 之后的事。

为什么值得有这么一条探针：异步依赖写错时的症状是**沉默的**。生成器写成普通函数、
``yield`` 前面忘了 ``async``、``finally`` 里漏了 ``await``——都不会报语法错，只会让某段
收尾悄悄不执行。所以这里断言的是三件事一起成立：响应一致（查询真的跑过）、两边记下的
SQL 一致（跑的是同一个查询）、会话被关（收尾真的执行）。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.async_session import get_async_db
from app.db.session import get_db
from tests.fake_db import AsyncRecordingSession, RecordingSession

_QUERY = text("SELECT id FROM knowledge_base WHERE tenant_id = :tid")


def _build_probe() -> FastAPI:
    """两条并排的探针路由：同一条 SQL、同一种响应形状，只有会话的栈不同。"""

    app = FastAPI()

    @app.get("/probe/sync")
    def sync_probe(db: Session = Depends(get_db)) -> dict[str, list[str]]:
        rows = db.execute(_QUERY, {"tid": "t-1"}).scalars().all()
        return {"ids": list(rows)}

    @app.get("/probe/async")
    async def async_probe(db: AsyncSession = Depends(get_async_db)) -> dict[str, list[str]]:
        rows = (await db.execute(_QUERY, {"tid": "t-1"})).scalars().all()
        return {"ids": list(rows)}

    return app


def test_the_async_probe_returns_what_the_sync_one_returns(monkeypatch) -> None:
    sync_session = RecordingSession(tenant_ids=["kb-1", "kb-2"])
    async_session = AsyncRecordingSession(tenant_ids=["kb-1", "kb-2"])
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: sync_session)
    monkeypatch.setattr("app.db.async_session.AsyncSessionLocal", lambda: async_session)

    with TestClient(_build_probe()) as client:
        sync_response = client.get("/probe/sync")
        async_response = client.get("/probe/async")

    assert sync_response.status_code == 200
    assert async_response.status_code == 200
    assert async_response.json() == sync_response.json() == {"ids": ["kb-1", "kb-2"]}

    # 同一个查询：两边记下的 SQL 逐字相同，不只是「响应碰巧一样」。
    assert async_session.statements == sync_session.statements

    # 请求级依赖的收尾：两个栈都在请求结束后关会话。
    assert sync_session.closed is True
    assert async_session.closed is True


def test_each_request_gets_its_own_async_session(monkeypatch) -> None:
    """请求级而不是进程级：两个请求 = 两个会话，各自关闭。"""

    created: list[AsyncRecordingSession] = []

    def factory() -> AsyncRecordingSession:
        session = AsyncRecordingSession(tenant_ids=["kb-1"])
        created.append(session)
        return session

    monkeypatch.setattr("app.db.async_session.AsyncSessionLocal", factory)

    with TestClient(_build_probe()) as client:
        assert client.get("/probe/async").status_code == 200
        assert client.get("/probe/async").status_code == 200

    assert len(created) == 2
    assert all(session.closed for session in created)


def test_the_async_dependency_still_closes_the_session_when_the_route_fails(monkeypatch) -> None:
    async_session = AsyncRecordingSession()
    monkeypatch.setattr("app.db.async_session.AsyncSessionLocal", lambda: async_session)

    app = FastAPI()

    @app.get("/probe/boom")
    async def boom(db: AsyncSession = Depends(get_async_db)) -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/probe/boom").status_code == 500

    assert async_session.closed is True
