"""第 14 步（#199）· 归属索引改成「一行一次的账」那条迁移的验收。

它和纯建表迁移不同：**它换表名、给存量表加列、换索引，还新建一张汇总表**。所以这里守五件事：

1. 改名的方向对（旧 → 新），且只在 ``upgrade`` 里发生一次；
2. 六列的**可空性**对：``key_id`` / ``endpoint`` / ``code`` 必须可空（老行补不出来），
   ``meter_r`` / ``meter_rr`` / ``meter_llm`` 必须 NOT NULL + ``server_default '0'``（新行必填）；
3. 索引换得对：旧的单列 ``tenant_id`` 索引删除，两条复合索引（都带 ``created_at``）建上，
   ``trace_id`` 的唯一索引改名保留；
4. ``usage_daily`` 的主键是五个维度、带 7 档直方图列，**主键列不许 nullable**
   （Postgres 不允许，而且 ``NULL != NULL`` 会让 rollup 的 upsert 失去幂等）；
5. ``downgrade`` 真的能对称回去——这条迁移是可逆的，不像删列那条。

全部离线：``op`` 是记录器，不需要 PostgreSQL。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

from app.models.usage_daily import LATENCY_BUCKET_COUNT, UsageDaily, bucket_of

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"
BUCKET_COLUMNS = tuple(f"lat_b{index}" for index in range(LATENCY_BUCKET_COUNT))


class _FakeOp:
    """记录 DDL 的替身：只实现这条迁移用到的八个方法。"""

    def __init__(self) -> None:
        self.renames: list[tuple[str, str]] = []
        self.added: dict[str, list[sa.Column]] = {}
        self.dropped_indexes: list[str] = []
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], bool]] = {}
        self.created_tables: dict[str, list[Any]] = {}
        self.dropped_tables: list[str] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self.executed: list[str] = []

    def rename_table(self, old: str, new: str) -> None:
        self.renames.append((old, new))

    def add_column(self, table: str, column: sa.Column) -> None:
        self.added.setdefault(table, []).append(column)

    def drop_index(self, name: str, table_name: str | None = None, **_kw: Any) -> None:
        self.dropped_indexes.append(name)

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        unique: bool = False,
        **_kw: Any,
    ) -> None:
        self.created_indexes[name] = (table_name, tuple(columns), unique)

    def create_table(self, name: str, *elements: Any, **_kw: Any) -> None:
        self.created_tables[name] = list(elements)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def drop_column(self, table: str, column: str) -> None:
        self.dropped_columns.append((table, column))

    def execute(self, statement: Any) -> None:
        self.executed.append(str(statement))


def _load() -> ModuleType:
    path = next(VERSIONS_DIR.glob("*_rename_to_usage_events.py"))
    spec = importlib.util.spec_from_file_location("t_usage_events_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*, upgrade: bool) -> _FakeOp:
    module = _load()
    recorder = _FakeOp()
    module.op = recorder
    module.upgrade() if upgrade else module.downgrade()
    return recorder


# ────────────────────────────────────────────────────────────── upgrade


def test_it_renames_the_index_to_usage_events() -> None:
    recorder = _run(upgrade=True)

    assert recorder.renames == [("retrieval_traces", "usage_events")]


def test_the_six_new_columns_have_the_nullability_the_backfill_cannot_fix() -> None:
    """老行补不出来的三列必须可空；新行必填的三本账必须 NOT NULL 且默认 0。"""

    recorder = _run(upgrade=True)
    columns = {column.name: column for column in recorder.added["usage_events"]}

    assert set(columns) == {"key_id", "endpoint", "code", "meter_r", "meter_rr", "meter_llm"}
    for name in ("key_id", "endpoint", "code"):
        assert columns[name].nullable is True, f"{name} 必须可空：老行没有这个信息，回填只能靠猜"
    for name in ("meter_r", "meter_rr", "meter_llm"):
        column = columns[name]
        assert column.nullable is False
        assert column.server_default is not None, "存量行要能直接落成 0，否则加列当场失败"


def test_the_indexes_switch_to_composite_ones_including_created_at() -> None:
    recorder = _run(upgrade=True)

    assert set(recorder.dropped_indexes) == {
        "ix_retrieval_traces_tenant_id",
        "ix_retrieval_traces_trace_id",
    }
    assert recorder.created_indexes["ix_usage_events_trace_id"] == (
        "usage_events",
        ("trace_id",),
        True,
    )
    # 两条读法各一条索引：租户视角、per-Key 视角；第二列都是 created_at。
    assert recorder.created_indexes["ix_usage_events_tenant_created"] == (
        "usage_events",
        ("tenant_id", "created_at"),
        False,
    )
    assert recorder.created_indexes["ix_usage_events_key_created"] == (
        "usage_events",
        ("key_id", "created_at"),
        False,
    )


def test_the_primary_key_constraint_is_renamed_too() -> None:
    """``rename_table`` 不会改约束名，不改就会在 ``\\d usage_events`` 里留一个旧表名。"""

    recorder = _run(upgrade=True)

    assert any("retrieval_logs_pkey" in statement for statement in recorder.executed)


def test_usage_daily_has_the_five_dimensions_and_seven_buckets() -> None:
    recorder = _run(upgrade=True)
    elements = recorder.created_tables["usage_daily"]

    columns = {element.name: element for element in elements if isinstance(element, sa.Column)}
    assert set(BUCKET_COLUMNS) <= set(columns), "分位数不能相加，7 档直方图一列都不能少"
    for name in ("requests", "ok_requests", "meter_r", "meter_rr", "meter_llm", "lat_sum_ms"):
        assert name in columns
        assert columns[name].nullable is False

    primary_key = next(
        element for element in elements if isinstance(element, sa.PrimaryKeyConstraint)
    )
    # 迁移里是按**列名**声明的（alembic 的惯用写法），约束还没挂到 Table 上，所以 ``.columns``
    # 是空的——未解析的列名在 ``_pending_colargs`` 里。读它是私有的，但这里是为了守住
    # 「五个维度、顺序不能变」，读到空也会当场失败，不会静默放过。
    declared = list(getattr(primary_key, "_pending_colargs", ()))
    assert declared == [
        "day",
        "tenant_id",
        "key_id",
        "endpoint",
        "code",
    ]
    # 主键列不许 nullable：Postgres 不允许，且 NULL != NULL 会让 upsert 失去幂等。
    for name in ("tenant_id", "key_id", "endpoint", "code"):
        assert columns[name].nullable is False


# ────────────────────────────────────────────────────────────── downgrade


def test_downgrade_puts_everything_back() -> None:
    recorder = _run(upgrade=False)

    assert recorder.dropped_tables == ["usage_daily"]
    assert recorder.dropped_columns == [
        ("usage_events", "meter_llm"),
        ("usage_events", "meter_rr"),
        ("usage_events", "meter_r"),
        ("usage_events", "code"),
        ("usage_events", "endpoint"),
        ("usage_events", "key_id"),
    ]
    assert recorder.dropped_indexes == [
        "ix_usage_events_key_created",
        "ix_usage_events_tenant_created",
        "ix_usage_events_trace_id",
    ]
    assert recorder.renames == [("usage_events", "retrieval_traces")]
    assert set(recorder.created_indexes) == {
        "ix_retrieval_traces_tenant_id",
        "ix_retrieval_traces_trace_id",
    }


# ────────────────────────────────────────────────────────── 模型与分档


def test_usage_daily_model_matches_the_migration() -> None:
    """迁移与模型各写一遍列名，最容易漂移；这里把它们钉在一起。"""

    table = UsageDaily.__table__

    assert tuple(column.name for column in table.primary_key) == (
        "day",
        "tenant_id",
        "key_id",
        "endpoint",
        "code",
    )
    assert table.c.day.type.python_type is date
    for name in BUCKET_COLUMNS:
        assert table.c[name].nullable is False


@pytest.mark.parametrize(
    ("latency_ms", "expected"),
    [
        (0, 0),
        (199, 0),
        (200, 1),  # 边界归下一档：正好 200 ms 不算「<200」
        (399, 1),
        (400, 2),
        (799, 3),
        (1000, 5),
        (1499, 5),
        (1500, 6),
        (99_000, 6),
    ],
)
def test_bucket_of_assigns_boundary_values_to_the_upper_bucket(
    latency_ms: int, expected: int
) -> None:
    assert bucket_of(latency_ms) == expected
