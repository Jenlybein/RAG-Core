"""#172 · 文档删掉时版本行跟着走（零外键的代价：由代码显式删）。

仓库里一张表都没有 ``ForeignKey``，版本表的 ``document_id`` 因此是裸 ``String(36)`` +
索引，「文档删除时版本行跟着走」不能交给库级 ``ON DELETE CASCADE``。它落在删文档那条
路径里：与清除切片、删文档行**同一笔事务**，由仓储层的一条 DELETE 完成。

两个入口各有一半用例：

* 仓储函数本身——在内存 SQLite 上按真实 SQL 跑：只删这篇文档的行、没行时不报错、
  而且**不提交**（提交点只有请求边界那一处）；
* 两个删文档的入口（数据面路由与控制面服务）——在各自的 ASGI 用例里断言「这一步真的
  发生了、顺序与提交次数没变」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.document_version import DocumentVersion

#: 仓储函数是异步面（请求路径上只有 ``AsyncSession``）。这里的库是同步 SQLite，于是用
#: 「同一个同步会话」的异步视图跑它——见 :class:`_AsyncSessionView`。
from app.repositories import document_version_repository


class _AsyncSessionView:
    """把同步 ``Session`` 包成异步面：仓储只用到 ``await db.execute(...)``。

    为什么不直接上 aiosqlite：这条用例要验的是「DELETE 打在哪张表、带什么条件、
    以及有没有提交」，同步会话就能回答，且能直接 ``rollback()`` 验「没提交」。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self._session.execute(*args, **kwargs)


@pytest.fixture
def version_db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    DocumentVersion.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _version(
    db: Session,
    *,
    version_id: str,
    document_id: str,
    version: int,
    status: str = "current",
) -> None:
    db.add(
        DocumentVersion(
            id=version_id,
            document_id=document_id,
            version=version,
            content="退款流程见附件。",
            checksum="0" * 64,
            status=status,
            created_at=datetime(2026, 11, 8, 0, 0, 0),
        )
    )


def _remaining(db: Session) -> list[tuple[str, int]]:
    rows = db.execute(
        select(DocumentVersion.document_id, DocumentVersion.version).order_by(
            DocumentVersion.document_id, DocumentVersion.version
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


@pytest.mark.asyncio
async def test_it_deletes_every_version_of_that_document_only(version_db: Session) -> None:
    """两篇文档、四行版本：删 A 之后只剩 B 的两行。"""

    _version(version_db, version_id="va1", document_id="doc-a", version=1, status="archived")
    _version(version_db, version_id="va2", document_id="doc-a", version=2)
    _version(version_db, version_id="vb1", document_id="doc-b", version=1, status="archived")
    _version(version_db, version_id="vb2", document_id="doc-b", version=2)
    version_db.commit()

    await document_version_repository.delete_for_document(
        _AsyncSessionView(version_db), "doc-a"
    )

    assert _remaining(version_db) == [("doc-b", 1), ("doc-b", 2)]


@pytest.mark.asyncio
async def test_it_is_a_no_op_when_the_document_has_no_versions(version_db: Session) -> None:
    """版本行不存在不是错误：删文档这条路不该因为「没有版本可删」而失败。"""

    _version(version_db, version_id="vb1", document_id="doc-b", version=1)
    version_db.commit()

    await document_version_repository.delete_for_document(
        _AsyncSessionView(version_db), "doc-never-versioned"
    )

    assert _remaining(version_db) == [("doc-b", 1)]


@pytest.mark.asyncio
async def test_it_does_not_commit(version_db: Session) -> None:
    """提交点只有请求边界那一处：仓储删完不提交，回滚能把这批行全部还回来。"""

    _version(version_db, version_id="va1", document_id="doc-a", version=1)
    version_db.commit()

    await document_version_repository.delete_for_document(
        _AsyncSessionView(version_db), "doc-a"
    )
    assert _remaining(version_db) == [], "DELETE 已经发出去了"

    version_db.rollback()

    assert _remaining(version_db) == [("doc-a", 1)], "仓储自己提交过就会在这里露出来"
