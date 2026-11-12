"""控制台会话数据访问：唯一能碰 sessions 表的一层。

只 flush，不 commit——事务由 Service 收口。行里没有可变字段（要改就换一行），所以这里
没有 update，只有「建一行」与「删一行」。

三个动作全在异步栈上（第 11 步票 #130）：控制面的登录 / 登出 / 身份判定与「我的视图」
都已经是 ``AsyncSession``，这一层因此没有同步面。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.console_session import ConsoleSession


async def create(db: AsyncSession, session: ConsoleSession) -> ConsoleSession:
    db.add(session)
    await db.flush()
    return session


async def get_by_id(db: AsyncSession, session_id: str) -> ConsoleSession | None:
    """按主键取会话行：身份判定的第 2 步（见 ``app/core/console_auth``）。"""

    return await db.get(ConsoleSession, session_id)


async def delete(db: AsyncSession, session: ConsoleSession) -> None:
    """删行 = 服务端登出。只删 cookie 而不删行，会留下一个「还能继续用」的会话。"""

    await db.delete(session)
