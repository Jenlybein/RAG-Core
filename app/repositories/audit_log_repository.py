"""审计数据访问：唯一能碰 audit_logs 表的一层。

只写不改：审计是账本，**没有 update 也没有 delete**——能被改的账本不算账本。

三个动作全是 ``async``（票 #129 / #130）：控制面的审计列表是这张表唯一的读者，而写它的
两条路径（控制面各写入口、数据面上传）也都在异步栈上——搬一次就搬完，这里没有同步面。
分页必须在 SQL 里做（``LIMIT`` / ``OFFSET``），不是在应用层取全量再切片：审计表只会一直
变长，而「先取全量」的实现还会让 ``offset`` 的语义在数据量大一点时悄悄失效（它的代价随
历史线性上升，测试里永远看不出来）。
"""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


async def create(db: AsyncSession, entry: AuditLog) -> AuditLog:
    """写一条账本行（只 flush，不提交——它必须与业务写入同一笔事务）。"""

    db.add(entry)
    await db.flush()
    return entry


async def list_page(
    db: AsyncSession, *, limit: int, offset: int, action: str | None = None
) -> list[AuditLog]:
    """按时间倒序取「第 ``offset`` 条之后」的 ``limit`` 条；``limit`` / ``offset`` 由调用方给。

    ``action`` 是**精确匹配**（不是前缀）：动作名是有限枚举（``plan.update`` / ``key.issue``
    / ``model.reveal`` …），精确匹配让「筛 ``key.issue``」不会顺手把 ``key_policy.update``
    带进来；面板的筛选值直接取这些枚举，也就不存在「前缀写一半」的模糊地带。
    """

    statement = (
        _filtered(action).order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    )
    return list((await db.execute(statement)).scalars().all())


async def count(db: AsyncSession, *, action: str | None = None) -> int:
    """**这一筛选条件下**的总条数；不带 ``action`` 时就是全表条数。

    前端据此算页数（``ceil(total / limit)``）。返回「筛选后的总数」而不是「全表总数」：
    分页控件跟着筛选结果走，拿全表总数算出来的页数会让最后一页出现空页。
    """

    statement = select(func.count()).select_from(AuditLog)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    return int((await db.execute(statement)).scalar_one())


def _filtered(action: str | None) -> Select[tuple[AuditLog]]:
    """「哪些行」的唯一判定：``action`` 为空就是整张表，否则按动作名精确筛。"""

    statement = select(AuditLog)
    return statement if action is None else statement.where(AuditLog.action == action)
