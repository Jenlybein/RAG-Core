"""表 10：audit_logs —— 控制面每一次改动的账本。

一张账本只回答一个问题：**谁、什么时候、把什么改成了什么**。所以它是「动作名 + 目标 +
改前快照 + 改后快照」，而不是「把整行复述一遍」：快照只留这次改动涉及的字段，事后既能
看出改了哪几项，也不会把别的字段的历史混进来。

- ``actor_id`` **可空**：播种、回填、引导脚本这类系统动作没有「人」，硬塞一个假账号会让
  事后追溯读到一条查无此人的记录，比留空更糟；
- ``action`` 建索引：审计列表按动作名筛是最常见的读法；
- 两份快照都可空：一条新增只有 ``after``，一条删除只有 ``before``，两种动作共用同一张表，
  不必为「删除」再造一张只能删不能改的表。

列名带 ``_snapshot`` 后缀而不是裸的 ``before`` / ``after``：``BEFORE`` 在 PostgreSQL 里是
触发器关键字，而且裸写这两个词在 SQL 里读起来像半句话。

不带外键、不建关系——审计要能在被审计的行被删掉之后仍然读得懂。
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 可空：系统动作没有 actor。**不填一个不存在的账号 id 来「凑齐」这一列**。
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    # 目标用「类型:标识」的裸字符串（``plan:2`` / ``account:3f2c…``），不加外键也不拆列：
    # 目标被删掉之后，这条审计仍然要能读出「当时动的是哪一个」。
    target: Mapped[str] = mapped_column(String(128))
    before_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
