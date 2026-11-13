"""异步请求级依赖的最小 ASGI 探针：它真的在跑，收尾真的执行。

票 #126 写下这份探针时是**两栈并排**：同一条路由分别挂 ``get_db``（同步）与
``get_async_db``（异步），断言两个响应、两组 SQL、两边的关闭都一样。那个形状在第 11 步
票 #134 之后不可复现——同步的两个依赖 ``get_db`` / ``get_transaction`` 已删（同步引擎仍在，
只服务三处不在循环上的读者与 Alembic，见 ADR-0024 决策四）。删掉的是
**对照的那一半**，留下的是这一半：

* 请求级而不是进程级：两个请求 = 两个会话，各自关闭；
* 失败路径也关：路由抛错时会话照样在 ``finally`` 里 close。

为什么值得有这条探针：异步依赖写错时的症状是**沉默的**。生成器写成普通函数、``yield``
前面忘了 ``async``、``finally`` 里漏了 ``await``——都不会报语法错，只会让某段收尾悄悄
不执行。所以这里断言的是「收尾真的执行」，而不是「跑通了」。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.async_session import get_async_db
from tests.fake_db import AsyncRecordingSession

_QUERY = text("SELECT id FROM knowledge_base WHERE tenant_id = :tid")


def _build_probe() -> FastAPI:
    app = FastAPI()

    @app.get("/probe/async")
    async def async_probe(db: AsyncSession = Depends(get_async_db)) -> dict[str, list[str]]:
        rows = (await db.execute(_QUERY, {"tid": "t-1"})).scalars().all()
        return {"ids": list(rows)}

    return app


def test_the_probe_answers_with_what_the_session_returned(monkeypatch) -> None:
    """会话替身返回什么，响应就是什么——证明那次查询真的发到了会话上。"""

    async_session = AsyncRecordingSession(tenant_ids=["kb-1", "kb-2"])
    monkeypatch.setattr("app.db.async_session.AsyncSessionLocal", lambda: async_session)

    with TestClient(_build_probe()) as client:
        response = client.get("/probe/async")

    assert response.status_code == 200
    assert response.json() == {"ids": ["kb-1", "kb-2"]}
    # 发出去的是调用点写的那条 SQL，而不是别的东西碰巧凑出了同一个响应。
    assert async_session.statements == [str(_QUERY)]
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
