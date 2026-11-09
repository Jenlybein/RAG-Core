"""控制台会话数据访问：唯一能碰 sessions 表的一层。

只 flush，不 commit——事务由 Service 收口。行里没有可变字段（要改就换一行），所以这里
没有 update，只有「建一行」与「删一行」。
"""

from sqlalchemy.orm import Session

from app.models.console_session import ConsoleSession


def create(db: Session, session: ConsoleSession) -> ConsoleSession:
    db.add(session)
    db.flush()
    return session


def get_by_id(db: Session, session_id: str) -> ConsoleSession | None:
    return db.get(ConsoleSession, session_id)


def delete(db: Session, session: ConsoleSession) -> None:
    """删行 = 服务端登出。只删 cookie 而不删行，会留下一个「还能继续用」的会话。"""

    db.delete(session)
