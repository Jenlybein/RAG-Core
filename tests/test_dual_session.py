"""双栈期的会话桥（``app/db/dual_session``）：同一段异步代码，两种会话都真的执行了。

这一层存在的理由见模块文档：入库 / 索引链路已经换成 ``AsyncSession``，控制面与运维脚本
暂时还是同步会话，而两边**必须是同一份实现**。三条契约：

1. 同步会话：``execute`` / ``flush`` / ``get`` 真的落到那个会话上（不是被丢掉）；
2. 异步会话（含**异步替身**）：同样真的执行，且协程被 ``await`` 掉——
   「忘了 await」在这里的形态是 ``RuntimeWarning`` 加「副作用没发生」，所以两个都要断言；
3. 返回值形状不变：``execute`` 给的结果能读 ``rowcount``（CAS 靠它），``get`` 给行。

第 2 条是这一层最容易被写错的地方：按 ``isinstance(db, AsyncSession)`` 分叉时，套件里的
异步假会话会被判成同步，``flush`` 的协程直接丢掉——用例仍然绿，而写入没发生。
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db import dual_session
from app.db.base import Base
from app.models.document import Document
from app.utils.status import DocumentStatus
from tests.fake_db import AsyncRecordingSession


class _AsyncDouble(AsyncRecordingSession):
    """异步替身：额外记下 ``flush`` 被真正 await 过几次。"""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1
        await super().flush()


@pytest.fixture
def sync_db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine, tables=[Document.__table__])
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_execute_runs_on_a_sync_session_and_reports_rowcount(sync_db: Session) -> None:
    sync_db.add(
        Document(
            id="doc-sync",
            tenant_id="t",
            kb_id="kb",
            title="sync",
            content="",
            source_type="text",
            status=DocumentStatus.PROCESSING,
        )
    )
    sync_db.commit()

    result = asyncio.run(
        dual_session.execute(
            sync_db,
            update(Document)
            .where(Document.id == "doc-sync")
            .values(status=DocumentStatus.SUCCESS)
            .execution_options(synchronize_session=False),
        )
    )

    assert result.rowcount == 1, "同步会话上也要能读 rowcount（CAS 判定靠它）"
    sync_db.commit()
    sync_db.expire_all()
    assert sync_db.get(Document, "doc-sync").status == DocumentStatus.SUCCESS


def test_execute_awaits_async_sessions() -> None:
    double = _AsyncDouble()

    result = asyncio.run(
        dual_session.execute(double, select(Document.id).where(Document.id == "doc-async"))
    )

    assert double.statements == [
        str(select(Document.id).where(Document.id == "doc-async"))
    ], "语句必须真的发到异步会话上"
    assert result.all() == []


def test_flush_is_awaited_on_async_sessions() -> None:
    double = _AsyncDouble()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(dual_session.flush(double))

    assert double.flushes == 1, "异步会话的 flush 必须被 await 掉，不能被丢掉"
    assert [w for w in caught if "never awaited" in str(w.message)] == []


def test_get_reads_through_both_session_kinds(sync_db: Session) -> None:
    sync_db.add(
        Document(
            id="doc-get",
            tenant_id="t",
            kb_id="kb",
            title="get",
            content="",
            source_type="text",
            status=DocumentStatus.PROCESSING,
        )
    )
    sync_db.commit()

    assert asyncio.run(dual_session.get(sync_db, Document, "doc-get")).id == "doc-get"

    class _DoubleWithRow(AsyncRecordingSession):
        async def get(self, _model: type, _ident: Any) -> Any:
            return "row"

    assert asyncio.run(dual_session.get(_DoubleWithRow(), Document, "doc-get")) == "row"
