"""#152 · 关系表：一行一条有向边，作用域含版本，两端都是「文档 id + 节 id」。

这里守的不是「表建出来了」，而是 ADR-0027 决策 1 逐项落进库：

1. **作用域含版本**：``tenant_id`` / ``kb_id`` / ``index_version`` 三列都在，边才能与切片
   在同一份构建产物里配对（ADR-0025 决策一）；
2. **两端都带文档 id**：只有节 id 的话，两份文档的「退款 > 流程」分不开；
3. **边型只有三种**：CHECK 约束里写着那三个值，第四种插不进来；
4. **同一条边不许重复插入**：八列唯一约束在库里，不靠调用方自觉；
5. **迁移与模型是同一份契约**：列名 / 约束名 / 索引名逐项相等。

不需要真数据库：``op`` 是一个有状态的记录器，于是「库里现在有哪些表 / 列 / 约束 / 索引」
都是可以逐项比对的事实（同一套写法见 ``tests/test_index_build_version_migration.py``）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票的迁移（文件名 = 12 位 revision + 这个 slug）。
SLUG = "create_section_edges"

#: 与 ``app/models/section_edge.py`` 逐字一致的十一列。
EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "tenant_id",
        "kb_id",
        "index_version",
        "source_document_id",
        "source_section_id",
        "target_document_id",
        "target_section_id",
        "relation_type",
        "metadata",
        "created_at",
    }
)

#: 唯一的八列：作用域 + 两端 + 边型。少一列都会让「同一条边」出现两种写法。
EXPECTED_UNIQUE_COLUMNS = (
    "tenant_id",
    "kb_id",
    "index_version",
    "source_document_id",
    "source_section_id",
    "target_document_id",
    "target_section_id",
    "relation_type",
)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """有状态的假 ``op``：维护「库里现在有哪些表 / 列 / 索引 / 约束」。"""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Any]] = {}
        self.constraints: dict[str, list[Any]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.executed: list[str] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, table_name: str, *args: Any, **_kwargs: Any) -> None:
        assert table_name not in self.tables, f"表已存在：{table_name}"
        self.executed.append(f"ddl:create_table {table_name}")
        table = sa.Table(table_name, sa.MetaData(), *args)
        self.tables[table_name] = {
            column.name: {
                "nullable": bool(column.nullable),
                "type": str(column.type),
                "default": None if column.server_default is None else str(column.server_default),
            }
            for column in table.columns
        }
        self.constraints[table_name] = [
            item for item in table.constraints if not isinstance(item, sa.PrimaryKeyConstraint)
        ]

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        assert table_name in self.tables, table_name
        self.tables.pop(table_name)
        self.constraints.pop(table_name, None)
        self.executed.append(f"ddl:drop_table {table_name}")

    def create_index(
        self, index_name: str, table_name: str, columns: Any, *, unique: bool = False, **_kw: Any
    ) -> None:
        assert table_name in self.tables, table_name
        self.indexes[index_name] = {
            "table": table_name,
            "columns": tuple(columns),
            "unique": bool(unique),
        }

    def drop_index(self, index_name: str, table_name: str | None = None, **_kwargs: Any) -> None:
        spec = self.indexes.pop(index_name, None)
        assert spec is not None, f"要删的索引不存在：{index_name}"
        if table_name is not None:
            assert spec["table"] == table_name

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}")


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t152_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _revision_of(path: Path) -> str:
    match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 revision"
    return match.group("revision")


def _down_revision_of(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    match = _DOWN_REVISION_RE.search(text)
    assert match is not None, f"{path.name}: 找不到 down_revision"
    value = match.group("down_revision").strip()
    if value.startswith("None"):
        return None
    found = re.search(r"""['"]([^'"]+)['"]""", value)
    assert found is not None, f"{path.name}: 看不懂 down_revision={value}"
    return found.group(1)


def _migrate(direction: str = "upgrade") -> _RecordingOp:
    module = _load_migration(_migration_path(SLUG))
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()
    if direction == "downgrade":
        module.downgrade()
    return recorder


# ───────────────────────────────────────────────────────── 迁移链与命名


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path(SLUG)
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_chains_onto_the_single_current_head_without_branching() -> None:
    """接在**其余迁移的唯一 head** 上，且自己不另起一头。

    不写死 ``down_revision == e41b7c9d2a63``：#150 也在加迁移，真实接点是「当时的链尾」。
    要守的性质是「接得上、只有一条链、我这一条就是链尾」——分支头的症状是
    ``alembic upgrade head`` 只跑一条链，另一条悄悄没跑。
    """

    mine = _migration_path(SLUG)
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(path): path for path in paths}
    others = {revision for revision in revisions if revisions[revision] != mine}
    down_revisions = {
        _down_revision_of(revisions[revision])
        for revision in others
        if _down_revision_of(revisions[revision]) is not None
    }
    heads = others - down_revisions

    assert len(heads) == 1, f"其余迁移必须只有一条链，实际 head={sorted(heads)}"
    assert _down_revision_of(mine) == next(iter(heads)), "本票必须接在当前唯一 head 上"

    all_down = {_down_revision_of(path) for path in paths} - {None}
    assert set(revisions) - all_down == {_revision_of(mine)}, "本票应当就是唯一 head"


# ───────────────────────────────────────────────────────────── upgrade


def test_upgrade_creates_the_edge_table_with_exactly_the_enumerated_columns() -> None:
    recorder = _migrate()

    assert recorder.tables["section_edges"].keys() == EXPECTED_COLUMNS


def test_every_edge_column_is_not_null() -> None:
    """边没有可空列：作用域、两端、边型缺一个，这条边就不可解释（宁可写不进去）。"""

    recorder = _migrate()

    nullable = {name for name, spec in recorder.tables["section_edges"].items() if spec["nullable"]}
    assert nullable == set(), f"这些列不该可空：{sorted(nullable)}"


def test_upgrade_pins_the_three_relation_types_in_the_database() -> None:
    recorder = _migrate()
    checks = [
        item
        for item in recorder.constraints["section_edges"]
        if isinstance(item, sa.CheckConstraint)
    ]

    rendered = " ".join(str(item.sqltext) for item in checks)
    for relation in ("reference", "sibling", "parent"):
        assert relation in rendered, f"CHECK 里少了边型 {relation}"
    assert "fourth" not in rendered, "第四种边型不该被允许（ADR-0027：边型只有三种）"


def test_upgrade_forbids_self_loops() -> None:
    recorder = _migrate()
    checks = [
        item
        for item in recorder.constraints["section_edges"]
        if isinstance(item, sa.CheckConstraint)
    ]

    rendered = " ".join(str(item.sqltext) for item in checks)
    assert "source_section_id" in rendered and "target_section_id" in rendered


def test_upgrade_pins_the_eight_columns_of_edge_identity_as_unique() -> None:
    """同一条边不许重复插入：唯一约束的列必须正好是「作用域 + 两端 + 边型」。"""

    recorder = _migrate()
    uniques = [
        item
        for item in recorder.constraints["section_edges"]
        if isinstance(item, sa.UniqueConstraint)
    ]

    assert len(uniques) == 1, "边身份只有一种写法"
    assert tuple(uniques[0].columns.keys()) == EXPECTED_UNIQUE_COLUMNS


def test_upgrade_adds_both_reading_directions_as_indexes() -> None:
    """「从这一节出去」与「指向这一节」各一个索引；两个方向都要按作用域收窄。"""

    recorder = _migrate()
    indexes = {
        name: spec for name, spec in recorder.indexes.items() if spec["table"] == "section_edges"
    }

    assert set(indexes) == {
        "ix_section_edges_tenant_id",
        "ix_section_edges_scope_source",
        "ix_section_edges_scope_target",
    }
    scope = ("tenant_id", "kb_id", "index_version")
    assert indexes["ix_section_edges_scope_source"]["columns"][:3] == scope
    assert indexes["ix_section_edges_scope_target"]["columns"][:3] == scope
    assert indexes["ix_section_edges_scope_source"]["columns"][3:] == (
        "source_document_id",
        "source_section_id",
    )
    assert indexes["ix_section_edges_scope_target"]["columns"][3:] == (
        "target_document_id",
        "target_section_id",
    )


def test_upgrade_does_not_forbid_cross_kb_edges() -> None:
    """跨库引用是最常见的真实关系（ADR-0027 决策 5）：库里不许有「目标库等于源库」的约束。"""

    recorder = _migrate()
    rendered = " ".join(
        str(item.sqltext)
        for item in recorder.constraints["section_edges"]
        if isinstance(item, sa.CheckConstraint)
    )

    assert recorder.tables["section_edges"]["kb_id"]["nullable"] is False
    assert "kb_id" not in rendered, "跨库边必须能插进来，越权边界在查询侧"


# ─────────────────────────────────────────────────────────── downgrade


def test_downgrade_removes_the_table_and_every_index_of_it() -> None:
    recorder = _migrate("downgrade")

    assert "section_edges" not in recorder.tables
    assert not [name for name, spec in recorder.indexes.items() if spec["table"] == "section_edges"]


# ─────────────────────────────────────── 模型与迁移必须是同一份契约


def test_the_model_declares_the_same_table_as_the_migration() -> None:
    from app.models.section_edge import RELATION_TYPES, UNIQUE_EDGE_COLUMNS, SectionEdge

    assert SectionEdge.__tablename__ == "section_edges"
    assert {column.key for column in SectionEdge.__table__.columns} == EXPECTED_COLUMNS
    assert UNIQUE_EDGE_COLUMNS == EXPECTED_UNIQUE_COLUMNS
    assert RELATION_TYPES == ("reference", "sibling", "parent")

    unique = [
        constraint
        for constraint in SectionEdge.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    ]
    assert tuple(unique[0].columns.keys()) == EXPECTED_UNIQUE_COLUMNS

    checks = " ".join(
        str(constraint.sqltext)
        for constraint in SectionEdge.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    )
    for relation in RELATION_TYPES:
        assert relation in checks


def test_the_model_is_registered_for_alembic_autogenerate() -> None:
    import app.models  # noqa: F401 - 导入即注册
    from app.db.base import Base

    assert "section_edges" in Base.metadata.tables


@pytest.mark.parametrize("column", ["metadata"])
def test_the_evidence_column_is_jsonb_not_text(column: str) -> None:
    """证据是结构化的一段（命中原文 / 章节号 / 表达），不是拼好的一句话。"""

    from app.models.section_edge import SectionEdge

    assert str(SectionEdge.__table__.columns[column].type) == "JSONB"
