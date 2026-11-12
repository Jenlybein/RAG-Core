"""控制台会话数据访问：唯一能碰 sessions 表的一层。

只 flush，不 commit——事务由 Service 收口。行里没有可变字段（要改就换一行），所以这里
没有 update，只有「建一行」与「删一行」。

读（按 id 取会话行）在迁移期有两种会话：控制面的**读**端点用
:func:`get_by_id_async`（票 #129），登录 / 登出与「我的视图」仍用 :func:`get_by_id`。
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.console_session import ConsoleSession


def create(db: Session, session: ConsoleSession) -> ConsoleSession:
    db.add(session)
    db.flush()
    return session


def get_by_id(db: Session, session_id: str) -> ConsoleSession | None:
    return db.get(ConsoleSession, session_id)


async def get_by_id_async(db: AsyncSession, session_id: str) -> ConsoleSession | None:
    """:func:`get_by_id` 的异步孪生（控制面读的身份判定，票 #129）。"""

    return await db.get(ConsoleSession, session_id)


def delete(db: Session, session: ConsoleSession) -> None:
    """删行 = 服务端登出。只删 cookie 而不删行，会留下一个「还能继续用」的会话。"""

    db.delete(session)
