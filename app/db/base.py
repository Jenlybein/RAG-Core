"""SQLAlchemy 声明式基类。

所有表的 ORM 模型都继承它；alembic autogenerate 读的就是 Base.metadata。
"""

from datetime import datetime

from sqlalchemy.orm import DeclarativeBase


def now() -> datetime:
    """数据库列默认值用的时间源。"""

    return datetime.now()


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。"""

    pass
