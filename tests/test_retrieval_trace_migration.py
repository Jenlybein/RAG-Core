"""#101 · 归属索引的改名迁移：``retrieval_logs`` → ``retrieval_traces``。

不需要真数据库：把 ``op`` 换成一个**有状态**的记录器，先跑 initial 迁移铺出改造前的
四张业务表，再叠本票的改名迁移，于是「库变成了什么样」是可以逐列比对的事实，而不是
把迁移文本再抄一遍。

三条要守的东西：

1. **改名 + 只留十列**：旧表名不在库里，``trace_id`` 非空，其余原文 / 档位 / 用户列全没了；
2. **可升可降**：``downgrade()`` 跑完，表名、列集合与索引必须与改造前**逐项相等**；
3. **不碰别的表**：chunks / documents / knowledge_bases 一个列都不许变。

第 3 条与「迁移不做数据回填」不是一回事：本票的回填只写**结构列**（``kb_ids`` 来自
``kb_id``、``trace_id`` 来自 ``id`` 的确定性派生），原文列在 upgrade 里只被删除、从不被读。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票的改名迁移（文件名里刻意不写旧表名：迁移清单里不该再有它）。
SLUG = "rename_to_retrieval_traces"
#: 改名之前那条 head（``create profile_presets``）。
PREVIOUS_HEAD = "a7d2e8f4c615"
#: 改造前那张表长这样（见 initial 迁移）。
INITIAL_COLUMNS = frozenset({"id", "tenant_id", "kb_id", "query", "chunk_ids", "created_at"})
#: 改造后只允许这十列（与 ``app/models/retrieval_trace.py`` 逐字一致）。
EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "tenant_id",
        "trace_id",
        "kb_ids",
        "chunk_ids",
        "top_k",
        "latency_ms",
        "degraded_reason",
        "empty_reason",
        "created_at",
    }
)
#: 改造前那两条索引；改名不应把它们顺手留在库里的新表上。
INITIAL_INDEXES = frozenset({"ix_retrieval_logs_kb_id", "ix_retrieval_logs_tenant_id"})
EXPECTED_INDEXES = frozenset({"ix_retrieval_traces_tenant_id", "ix_retrieval_traces_trace_id"})

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)
_UNSET = object()


class _RecordingOp:
    """有状态的假 ``op``：维护「库里现在有哪些表 / 列 / 索引」，别的一律不接受。"""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.executed: list[str] = []

    # ── 迁移用到的几个 op ──

    def f(self, name: str) -> str:
        return name

    def create_table(self, table_name: str, *args: Any, **_kwargs: Any) -> None:
        self.tables[table_name] = {
            arg.name: _spec(arg) for arg in args if hasattr(arg, "name") and hasattr(arg, "type")
        }

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        self.tables.pop(table_name)

    def rename_table(self, old: str, new: str) -> None:
        assert old in self.tables, f"要改名的表不存在：{old}"
        assert new not in self.tables, f"目标表已存在：{new}"
        self.tables[new] = self.tables.pop(old)
        for spec in self.indexes.values():
            if spec["table"] == old:
                spec["table"] = new

    def add_column(self, table_name: str, column: Any) -> None:
        assert table_name in self.tables, table_name
        self.tables[table_name][column.name] = _spec(column)

    def drop_column(self, table_name: str, column_name: str, **_kwargs: Any) -> None:
        assert table_name in self.tables, table_name
        assert column_name in self.tables[table_name], f"{table_name}.{column_name} 不存在"
        self.tables[table_name].pop(column_name)

    def alter_column(
        self,
        table_name: str,
        column_name: str,
        *,
        existing_type: Any = _UNSET,
        nullable: Any = _UNSET,
        server_default: Any = _UNSET,
        **_kwargs: Any,
    ) -> None:
        spec = self.tables[table_name][column_name]
        if nullable is not _UNSET:
            spec["nullable"] = bool(nullable)
        if server_default is not _UNSET:
            spec["default"] = None if server_default is None else str(server_default)

    def create_index(self, index_name: str, table_name: str, columns: Any, **kwargs: Any) -> None:
        assert table_name in self.tables, table_name
        self.indexes[index_name] = {
            "table": table_name,
            "columns": tuple(columns),
            "unique": bool(kwargs.get("unique")),
        }

    def drop_index(self, index_name: str, table_name: str | None = None, **_kwargs: Any) -> None:
        spec = self.indexes.pop(index_name, None)
        assert spec is not None, f"要删的索引不存在：{index_name}"
        if table_name is not None:
            assert spec["table"] == table_name, (
                f"{index_name} 属于 {spec['table']}，不是 {table_name}"
            )

    def execute(self, statement: Any) -> None:
        self.executed.append(str(statement))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}")

    # ── 断言用的便利读法 ──

    def columns_of(self, table_name: str) -> frozenset[str]:
        return frozenset(self.tables[table_name])

    def indexes_of(self, table_name: str) -> frozenset[str]:
        return frozenset(name for name, spec in self.indexes.items() if spec["table"] == table_name)

    def snapshot(self) -> tuple[dict[str, frozenset[str]], dict[str, str]]:
        """除本票那张表之外的「库的形状」：列集合 + 每张表上的索引名。"""

        tables = {
            name: self.columns_of(name)
            for name in self.tables
            if name not in ("retrieval_logs", "retrieval_traces")
        }
        indexes = {
            name: spec["table"]
            for name, spec in self.indexes.items()
            if spec["table"] not in ("retrieval_logs", "retrieval_traces")
        }
        return tables, indexes


def _spec(column: Any) -> dict[str, Any]:
    default = getattr(column, "server_default", None)
    return {
        "type": str(column.type),
        "nullable": bool(column.nullable),
        "default": None if default is None else str(default),
    }


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t101_migration_{path.stem}", path)
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


def _initial_module() -> ModuleType:
    paths = sorted(VERSIONS_DIR.glob("*_initial.py"))
    assert len(paths) == 1
    return _load_migration(paths[0])


def _before_change() -> _RecordingOp:
    """改造前的库：只有 initial 迁移跑过。"""

    recorder = _RecordingOp()
    initial = _initial_module()
    initial.op = recorder
    initial.upgrade()
    assert recorder.columns_of("retrieval_logs") == set(INITIAL_COLUMNS)
    assert recorder.indexes_of("retrieval_logs") == set(INITIAL_INDEXES)
    return recorder


def _migrate(direction: str) -> _RecordingOp:
    """把本票的迁移跑一遍；``downgrade`` 总是**先升后降**（真实用法就是如此）。

    只跑 ``downgrade()`` 而不先 ``upgrade()`` 的话，记录器里根本没有新列与新索引，
    那条断言测的就成了「往空库里删东西」，而不是「退得回去」。
    """

    recorder = _before_change()
    module = _load_migration(_migration_path(SLUG))
    module.op = recorder
    module.upgrade()
    if direction == "downgrade":
        recorder.executed.clear()
        module.downgrade()
    return recorder


# ──────────────────────────────────────────────────────── 命名、链与 head 唯一性


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path(SLUG)
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_chains_onto_the_previous_head_and_is_now_the_only_head() -> None:
    path = _migration_path(SLUG)
    assert _down_revision_of(path) == PREVIOUS_HEAD

    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(candidate): candidate for candidate in paths}
    down_revisions = {_down_revision_of(candidate) for candidate in paths} - {None}
    heads = set(revisions) - down_revisions

    assert heads == {_revision_of(path)}, f"迁移链必须只有一个 head，实际 {sorted(heads)}"


# ────────────────────────────────────────────────────────────── upgrade：改名 + 十列


def test_upgrade_leaves_the_table_with_exactly_the_enumerated_columns() -> None:
    recorder = _migrate("upgrade")

    assert "retrieval_logs" not in recorder.tables, "旧表名不该还留在库里"
    assert recorder.columns_of("retrieval_traces") == set(EXPECTED_COLUMNS)


def test_upgrade_drops_every_column_that_could_carry_text() -> None:
    """原文、改写句、检索句、用户与档位一概不存在——这张表只存结构。"""

    recorder = _migrate("upgrade")
    columns = recorder.columns_of("retrieval_traces")

    for gone in ("query", "kb_id"):
        assert gone not in columns
    for word in ("query", "effective", "search", "retrieved", "content", "user_id", "profile"):
        assert not [name for name in columns if word in name], f"不该有 {word} 一类的列"


def test_upgrade_makes_trace_id_not_null_without_leaving_a_placeholder_default() -> None:
    """``trace_id`` 是 NOT NULL，且**不是**靠一个永久默认值撑住的（回了填就收回默认）。"""

    recorder = _migrate("upgrade")
    spec = recorder.tables["retrieval_traces"]["trace_id"]

    assert spec["nullable"] is False
    assert spec["default"] is None


def test_upgrade_replaces_the_old_indexes_with_identity_ones() -> None:
    recorder = _migrate("upgrade")

    assert recorder.indexes_of("retrieval_traces") == set(EXPECTED_INDEXES)
    assert not [name for name in recorder.indexes if name in INITIAL_INDEXES], (
        "改名不会重命名索引，旧名字必须显式删掉"
    )
    assert recorder.indexes["ix_retrieval_traces_trace_id"]["unique"] is True, (
        "一行一次检索：trace_id 唯一，才谈得上「一一对应」"
    )


def test_upgrade_backfills_structure_columns_and_never_reads_the_question_text() -> None:
    """存量行要能带着新列活下来，但回填只碰结构列：原文列只删、不读、不搬。"""

    recorder = _migrate("upgrade")

    assert recorder.executed, "存量行必须被回填，否则 NOT NULL 加不上"
    statements = " ".join(recorder.executed).lower()
    assert "kb_ids" in statements and "trace_id" in statements
    assert "query" not in statements, "原文列不许出现在任何一条回填语句里"


def test_upgrade_touches_no_other_table() -> None:
    before, _ = _before_change().snapshot()
    after, after_indexes = _migrate("upgrade").snapshot()

    assert after == before, "本票只动归属索引这一张表"
    assert after_indexes == {
        name: spec["table"]
        for name, spec in _before_change().indexes.items()
        if spec["table"] not in ("retrieval_logs", "retrieval_traces")
    }


# ──────────────────────────────────────────────────────────────── downgrade：退得回去


def test_downgrade_restores_the_previous_shape_exactly() -> None:
    """回滚之后表名、列集合、索引名逐项等于改造前——不是「大致能跑」。"""

    recorder = _migrate("downgrade")

    assert "retrieval_traces" not in recorder.tables
    assert recorder.columns_of("retrieval_logs") == set(INITIAL_COLUMNS)
    assert recorder.indexes_of("retrieval_logs") == set(INITIAL_INDEXES)


def test_downgrade_restores_the_not_null_columns_it_had_to_recreate() -> None:
    """``kb_id`` / ``query`` 当初是 NOT NULL，退回去也要是；回滚不是「能删就行」。"""

    recorder = _migrate("downgrade")
    columns = recorder.tables["retrieval_logs"]

    assert columns["kb_id"]["nullable"] is False
    assert columns["query"]["nullable"] is False


def test_downgrade_fills_the_question_column_with_a_placeholder_only() -> None:
    """原文已经永久离开业务库：回滚只能给占位空串，不能凭空造出问句。"""

    recorder = _migrate("downgrade")
    statements = [sql for sql in recorder.executed if "query" in sql.lower()]

    assert statements, "NOT NULL 回滚前必须给存量行一个值"
    assert all("select" not in sql.lower() for sql in statements), (
        "占位值不能来自别的表（更不许来自观测栈）"
    )


def test_downgrade_leaves_no_new_column_behind() -> None:
    recorder = _migrate("downgrade")
    columns = recorder.columns_of("retrieval_logs")

    for added in EXPECTED_COLUMNS - INITIAL_COLUMNS:
        assert added not in columns, f"{added} 是本次新增的列，回滚后不该还在"
