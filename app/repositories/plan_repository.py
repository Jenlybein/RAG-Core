"""套餐数据访问：唯一能碰 plans 表的一层。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。

**两种会话并存是迁移期的形状**（票 #129 起）：控制面的**读**已经换到 ``AsyncSession``
（:func:`list_all`），而写路径与判定内核仍在同步会话上（:func:`get_by_id` /
:func:`get_by_code` / :func:`count`）。同一个操作**不会**同时有两份实现——搬一条删一条，
同步版在 #130 / #127 搬完它的调用点之后消失（见 ``app/db/async_session.py`` 的模块文档）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.plan import Plan


def create(db: Session, plan: Plan) -> Plan:
    db.add(plan)
    db.flush()
    return plan


def get_by_id(db: Session, plan_id: int) -> Plan | None:
    return db.get(Plan, plan_id)


def get_by_code(db: Session, code: str) -> Plan | None:
    """按档位码取。**只有三档是种子**，库里少一档时调用方必须当错误处理。"""

    return db.query(Plan).filter(Plan.code == code).first()


async def list_all(db: AsyncSession) -> list[Plan]:
    """管理员看全部套餐（控制面读，票 #129）：按 id 排序只为让两次读的结果稳定。"""

    return list((await db.execute(select(Plan).order_by(Plan.id))).scalars().all())


def count(db: Session) -> int:
    """数行数而不是取回全部：播种只关心「表是不是空的」。"""

    return db.query(Plan).count()
