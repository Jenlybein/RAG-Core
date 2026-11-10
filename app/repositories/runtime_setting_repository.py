"""运行时配置覆盖的数据访问：唯一能碰 ``runtime_settings`` 表的一层。

只 flush，不 commit——事务由 Service 收口（与既有仓储同一套规矩）。

「写入」只有 :func:`upsert` 一条路：配置面板是**按键覆盖**的语义（同一个键被改两次，
库里必须仍是一行），留一个 ``insert`` 与一个 ``update`` 两个入口，就等于指望每个调用点
自己记得先查一次再决定走哪条——而漏查的那一次会撞主键冲突，把一次普通保存变成 500。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.runtime_setting import RuntimeSetting


def list_all(db: Session) -> list[RuntimeSetting]:
    """全部覆盖行。这张表**天生很小**（只存被显式覆盖的键），所以一次取回不设上限。"""

    return db.query(RuntimeSetting).order_by(RuntimeSetting.key).all()


def get_by_key(db: Session, key: str) -> RuntimeSetting | None:
    return db.query(RuntimeSetting).filter(RuntimeSetting.key == key).first()


def upsert(
    db: Session,
    *,
    key: str,
    value: Any,
    updated_by: str,
    updated_at: datetime,
) -> RuntimeSetting:
    """按键写入；已存在就改这一行。``updated_at`` 由调用方给，不靠列的默认值。

    时间戳显式传入的理由与审计表同一条：它是「这次改动发生在哪一刻」的唯一证据，
    让它在落库路径之外（测试里的假会话、批量保存）也能被读到。
    """

    row = get_by_key(db, key)
    if row is None:
        row = RuntimeSetting(
            key=key, value=value, updated_by=updated_by, updated_at=updated_at
        )
        db.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
        row.updated_at = updated_at
    db.flush()
    return row
