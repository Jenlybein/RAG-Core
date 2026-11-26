"""票 #74：五张身份表的形状。

表结构本身就是这一票的交付物，所以直接对 ``Base.metadata`` 断言——迁移文件与模型
拿的是同一份元数据，这里守的形状就是库里的形状。

三条风格红线单独立项守：**裸字符串 id、不加外键、没有 ORM 关系**。它们是「与既有四张
业务表风格一致」的可执行定义；一旦有人加了个 ``ForeignKey``，就有了两套引用风格，
而既有表上的 ``tenant_id`` 列既没有外键也不会有。
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKeyConstraint, inspect
from sqlalchemy.dialects.postgresql import JSONB

import app.models as models_package
from app.db.base import Base, now
from app.models import Account, ApiKey, KeyPolicy, Plan, Tenant
from app.models.plan import PLAN_BUSINESS_COLUMNS

IDENTITY_TABLES = ("tenants", "plans", "accounts", "key_policies", "api_keys")
IDENTITY_MODELS = (Tenant, Plan, Account, KeyPolicy, ApiKey)


def _default_arg(model: type[Base], column: str) -> object:
    """取列的 Python 侧默认值；SQLAlchemy 会把零参可调用包一层 ``__wrapped__``。"""

    arg = model.__table__.c[column].default.arg
    return getattr(arg, "__wrapped__", arg)


def _onupdate_arg(model: type[Base], column: str) -> object:
    arg = model.__table__.c[column].onupdate.arg
    return getattr(arg, "__wrapped__", arg)


# ────────────────────────────────────────────────────────────────── 注册与风格


def test_the_five_tables_are_registered_in_metadata() -> None:
    assert set(IDENTITY_TABLES) <= set(Base.metadata.tables)


@pytest.mark.parametrize("model", IDENTITY_MODELS, ids=lambda model: model.__name__)
def test_every_model_is_exported_from_the_models_package(model: type[Base]) -> None:
    """``app.models`` 是 alembic 的入口：没导出的模型等于不存在于 metadata。"""

    assert model.__name__ in models_package.__all__
    assert getattr(models_package, model.__name__) is model


@pytest.mark.parametrize("model", IDENTITY_MODELS, ids=lambda model: model.__name__)
def test_no_foreign_keys_anywhere(model: type[Base]) -> None:
    table = model.__table__
    assert not table.foreign_keys
    assert not [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]


@pytest.mark.parametrize("model", IDENTITY_MODELS, ids=lambda model: model.__name__)
def test_no_orm_relationships(model: type[Base]) -> None:
    """引用关系靠应用层校验：有了 relationship 就等于有了「加载别人租户」的捷径。"""

    assert len(inspect(model).relationships) == 0


# ─────────────────────────────────────────────────────────────────────── tenants


def test_tenants_key_is_the_existing_tenant_identifier() -> None:
    """主键沿用四张业务表里已经在用的 ``tenant_id`` 值，所以不需要搬数据。"""

    table = Tenant.__table__
    assert [column.name for column in table.primary_key] == ["id"]
    assert table.c.id.type.length == 64
    assert table.c.name.type.length == 128


def test_tenants_points_at_a_plan_without_a_foreign_key() -> None:
    table = Tenant.__table__
    assert table.c.plan_id.type.python_type is int
    assert not table.c.plan_id.nullable, "租户必须有套餐：上限为空的租户不是一种状态"
    assert not table.c.plan_id.foreign_keys


def test_tenants_status_defaults_to_active() -> None:
    assert Tenant.__table__.c.status.type.length == 32
    assert _default_arg(Tenant, "status") == "active"


def test_tenants_timestamps_come_from_the_shared_now_helper() -> None:
    assert _default_arg(Tenant, "created_at") is now
    assert _default_arg(Tenant, "updated_at") is now
    assert _onupdate_arg(Tenant, "updated_at") is now


# ───────────────────────────────────────────────────────────────────────── plans


def test_plans_has_every_business_column() -> None:
    """面板上会出现输入框的 16 列一列都不能只活在代码里（第 13 步删了 5 个档位列）。"""

    table = Plan.__table__
    business = tuple(
        column.name
        for column in table.columns
        if column.name not in {"id", "code", "name", "updated_at"}
    )
    assert set(business) == set(PLAN_BUSINESS_COLUMNS)
    assert len(business) == 17
    for column in PLAN_BUSINESS_COLUMNS:
        assert not table.c[column].nullable, f"{column} 不允许为空"


def test_plans_identity_is_an_integer_primary_key_with_a_unique_code() -> None:
    table = Plan.__table__
    assert [column.name for column in table.primary_key] == ["id"]
    assert table.c.id.type.python_type is int
    assert table.c.code.type.length == 32
    assert table.c.code.unique is True
    assert table.c.code.index is True


def test_plans_no_longer_has_allowed_profiles() -> None:
    """第 13 步：档位白名单列已物理删除。"""

    assert "allowed_profiles" not in Plan.__table__.c


def test_plans_has_only_updated_at() -> None:
    """套餐是系统级配置：被关心的是「上一次被谁改成什么」，不是何时建出来。"""

    columns = set(Plan.__table__.c.keys())
    assert "updated_at" in columns
    assert "created_at" not in columns
    assert _onupdate_arg(Plan, "updated_at") is now


# ────────────────────────────────────────────────────────────────────── accounts


def test_accounts_belong_to_a_tenant_but_may_belong_to_none() -> None:
    """管理员不属于任何租户：``tenant_id`` 必须可空。"""

    table = Account.__table__
    assert table.c.id.type.length == 36
    assert table.c.tenant_id.type.length == 64
    assert table.c.tenant_id.nullable is True
    assert table.c.tenant_id.index is True


def test_accounts_username_is_unique_and_password_is_stored_as_a_hash() -> None:
    table = Account.__table__
    assert table.c.username.type.length == 64
    assert table.c.username.unique is True
    assert table.c.password_hash.type.length == 255
    assert _default_arg(Account, "role") == "user"
    assert _default_arg(Account, "status") == "active"


# ────────────────────────────────────────────────────────────────── key_policies


def test_key_policies_shape() -> None:
    table = KeyPolicy.__table__
    assert table.c.id.type.length == 36
    assert table.c.tenant_id.type.length == 64
    assert table.c.tenant_id.index is True
    assert table.c.name.type.length == 128
    assert isinstance(table.c.allowed_kb_ids.type, JSONB)
    # 第 13 步：三项参数搬成真列，档位/JSONB 载体列已物理删除。
    assert "custom_options" not in table.c
    assert "profile" not in table.c
    for column in ("retrieval_mode", "retrieval_top_k", "retrieval_rerank"):
        assert column in table.c


def test_key_policy_switches_start_closed_but_result_shape_starts_open() -> None:
    """授权位默认不全开（加工端点最贵），结果形状默认开（与既有响应一致）。"""

    for column in ("research_allowed", "rerank_allowed", "rewrite_allowed", "decompose_allowed"):
        assert _default_arg(KeyPolicy, column) is False, column
    assert _default_arg(KeyPolicy, "retrieve_allowed") is True, "基线检索默认开"
    for column in ("include_parent", "expand_table_rows"):
        assert _default_arg(KeyPolicy, column) is True, column
    assert _default_arg(KeyPolicy, "retrieval_mode") == "vector"
    assert _default_arg(KeyPolicy, "retrieval_top_k") == 5
    assert _default_arg(KeyPolicy, "retrieval_rerank") is False


def test_key_policies_limits_are_integers() -> None:
    table = KeyPolicy.__table__
    for column in (
        "retrieve_qps",
        "retrieve_daily",
        "rerank_qps",
        "rerank_daily",
        "llm_qps",
        "llm_daily",
    ):
        assert table.c[column].type.python_type is int


def test_the_three_book_quotas_default_to_zero() -> None:
    """第 13 步：三本账的新增两本默认 0 = 未启用（旧 Key 升级不被误拒）。"""

    assert _default_arg(KeyPolicy, "rerank_qps") == 0
    assert _default_arg(KeyPolicy, "rerank_daily") == 0
    assert _default_arg(KeyPolicy, "llm_qps") == 0
    assert _default_arg(KeyPolicy, "llm_daily") == 0


# ─────────────────────────────────────────────────────────────────────── api_keys


def test_api_keys_shape() -> None:
    table = ApiKey.__table__
    assert table.c.id.type.length == 36
    assert table.c.tenant_id.type.length == 64
    assert table.c.tenant_id.index is True
    assert table.c.policy_id.type.length == 36
    assert table.c.policy_id.index is True
    # 库里只有哈希与末 5 位：原文一列都没有，于是「按原文一击命中」这条路不复存在。
    assert "key_value" not in table.c
    assert table.c.key_hash.type.length == 255
    assert table.c.key_tail.type.length == 16
    # 哈希不参与查找（行定位靠主键），加唯一索引等于给一个没人查的列建索引。
    assert table.c.key_hash.unique is not True
    assert table.c.key_hash.index is not True
    assert table.c.label.type.length == 128
    assert table.c.label.nullable is True
    assert table.c.last_used_at.nullable is True
    assert _default_arg(ApiKey, "status") == "active"


def test_api_keys_have_a_creation_time_but_no_update_time() -> None:
    """Key 的行本身不会被改（停用改的是 status），只有创建时间有语义。"""

    columns = set(ApiKey.__table__.c.keys())
    assert "created_at" in columns
    assert "updated_at" not in columns
    assert _default_arg(ApiKey, "created_at") is now
