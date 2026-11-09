"""票 #80：两张控制面表（会话 / 审计）的形状。

表结构本身就是这一票的交付物的一半，所以直接对 ``Base.metadata`` 断言——迁移与模型
拿的是同一份元数据，这里守的形状就是库里的形状。

三条风格红线与身份五表同一条口径：**裸字符串 id、不加外键、没有 ORM 关系**。
会话表另外多守一条：**凭证是随机不可猜的标识本身**，里面不许出现任何「能推导出账号
或口令」的东西。
"""

from __future__ import annotations

from sqlalchemy import DateTime, ForeignKeyConstraint, inspect
from sqlalchemy.dialects.postgresql import JSONB

import app.models as models_package
from app.db.base import Base, now
from app.models import AuditLog, ConsoleSession

CONSOLE_TABLES = ("sessions", "audit_logs")
CONSOLE_MODELS = (ConsoleSession, AuditLog)


def _default_arg(model: type[Base], column: str) -> object:
    """取列的 Python 侧默认值；SQLAlchemy 会把零参可调用包一层 ``__wrapped__``。"""

    arg = model.__table__.c[column].default.arg
    return getattr(arg, "__wrapped__", arg)


# ────────────────────────────────────────────────────────────────── 注册与风格


def test_both_console_tables_are_registered_in_metadata() -> None:
    assert set(CONSOLE_TABLES) <= set(Base.metadata.tables)


def test_both_models_are_exported_from_the_models_package() -> None:
    """``app.models`` 是 alembic 的入口：没导出的模型等于不存在于 metadata。"""

    for model in CONSOLE_MODELS:
        assert model.__name__ in models_package.__all__
        assert getattr(models_package, model.__name__) is model


def test_no_foreign_keys_anywhere() -> None:
    """引用关系靠应用层校验：加了外键就有两套引用风格。"""

    for model in CONSOLE_MODELS:
        table = model.__table__
        assert not table.foreign_keys, model.__name__
        assert not [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        ], model.__name__


def test_no_orm_relationships() -> None:
    for model in CONSOLE_MODELS:
        assert len(inspect(model).relationships) == 0, model.__name__


# ──────────────────────────────────────────────────────────────────── sessions


def test_a_session_is_an_opaque_random_id_that_belongs_to_an_account() -> None:
    """主键就是凭证本身：随机的不可猜标识，不存口令、不存任何可推导的东西。"""

    table = ConsoleSession.__table__
    assert [column.name for column in table.primary_key] == ["id"]
    assert table.c.id.type.length == 64
    assert table.c.account_id.type.length == 36
    assert table.c.account_id.index is True
    assert not table.c.account_id.nullable


def test_a_session_has_an_expiry_and_a_creation_time() -> None:
    table = ConsoleSession.__table__
    assert isinstance(table.c.expires_at.type, DateTime)
    assert not table.c.expires_at.nullable
    assert _default_arg(ConsoleSession, "created_at") is now


def test_a_session_row_carries_no_secret_beyond_its_own_id() -> None:
    """会话行里没有口令、没有 token 副本：多一份就多一处泄露面。"""

    leaked = {
        "password",
        "password_hash",
        "token",
        "secret",
        "api_key",
    }
    assert not leaked & set(ConsoleSession.__table__.c.keys())


# ────────────────────────────────────────────────────────────────── audit_logs


def test_an_audit_log_records_actor_action_target_and_two_snapshots() -> None:
    table = AuditLog.__table__
    assert [column.name for column in table.primary_key] == ["id"]
    assert table.c.id.type.length == 36
    assert table.c.action.type.length == 64
    assert table.c.target.type.length == 128
    assert isinstance(table.c.before_snapshot.type, JSONB)
    assert isinstance(table.c.after_snapshot.type, JSONB)
    assert _default_arg(AuditLog, "created_at") is now


def test_the_actor_may_be_absent_because_system_actions_have_no_actor() -> None:
    assert AuditLog.__table__.c.actor_id.nullable is True
    assert AuditLog.__table__.c.actor_id.type.length == 36


def test_the_action_name_is_indexed_because_every_listing_filters_on_it() -> None:
    assert AuditLog.__table__.c.action.index is True


def test_both_snapshots_are_optional_so_creations_and_deletions_fit_one_shape() -> None:
    """一条新增只有 after，一条删除只有 before：两种动作共用同一张表。"""

    assert AuditLog.__table__.c.before_snapshot.nullable is True
    assert AuditLog.__table__.c.after_snapshot.nullable is True
