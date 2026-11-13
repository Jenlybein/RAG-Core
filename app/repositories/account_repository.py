"""账号数据访问：唯一能碰 accounts 表的一层。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。

**两条读的会话类型不同**（第 11 步票 #130）：控制面（登录、``/console/me``、账号清单）
全部在异步栈上，所以 :func:`get_by_id` / :func:`list_all` 是 ``async``；
:func:`get_by_username_async` 有同步孪生，因为运维脚本 ``scripts/create_admin.py``
还在同步会话上跑（它拿不到事件循环，也不该为一个「有没有这个用户名」变成异步）。
同步与异步两份并存：同步那份服务运维脚本，异步那份带 ``_async`` 后缀服务请求路径（约定见 ``app/db/async_session.py``）。

``get_by_username`` 是登录的唯一入口：登录先按用户名定位账号，再验口令；查不到就走
「等时假校验」那条路（见 ``app.core.passwords.dummy_verify``）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.account import Account


def create(db: Session, account: Account) -> Account:
    db.add(account)
    db.flush()
    return account


async def get_by_id(db: AsyncSession, account_id: str) -> Account | None:
    """按主键取一行（控制面的身份判定与「我的视图」都走它）。"""

    return await db.get(Account, account_id)


def get_by_username(db: Session, username: str) -> Account | None:
    """登录入口的**同步**那一份（运维脚本用；请求路径用 :func:`get_by_username_async`）。"""

    return db.query(Account).filter(Account.username == username).first()


async def get_by_username_async(db: AsyncSession, username: str) -> Account | None:
    """:func:`get_by_username` 的异步孪生（控制面登录，票 #130）。"""

    statement = select(Account).where(Account.username == username)
    return (await db.execute(statement)).scalars().first()


async def list_all(db: AsyncSession) -> list[Account]:
    """管理员视角的账号清单：按 id 排序只为让两次读的结果稳定。"""

    return list((await db.execute(select(Account).order_by(Account.id))).scalars().all())
