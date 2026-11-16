"""#179 · 归属索引「文档 → 稿号」映射列的迁移。

不需要真 PostgreSQL：把 ``op`` 换成一个**有状态**的记录器，先按本迁移之前的真实形状摆好
``retrieval_traces``（十一列），再叠本票的迁移，于是「库变成了什么样」是可以逐列比对的
事实，而不是把迁移文本再抄一遍（先例：``test_retrieval_trace_migration.py``）。

三条要守的东西：

1. **只加一列**：``doc_versions`` 是 JSONB、可空、**没有** server_default；
2. **不碰别的**：其余十一列一个都不许变，也不许新增索引（这一列不是检索条件）；
3. **可升可降**：``downgrade()`` 跑完，列集合与改造前逐项相等。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from app.models.retrieval_trace import RetrievalTrace

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票的迁移（文件名 = 12 位 revision + 这个 slug）。
SLUG = "add_trace_doc_versions"
#: 本迁移接在 #172 的版本表后面（integration tip 上的旧 head）。
PREVIOUS_HEAD = "7c2e5a9b4d31"

#: 改造前的列（十一列，逐列抄自 ``app/models/retrieval_trace.py`` 在本票之前的样子）。
BEFORE_COLUMNS = (
    "id",
    "tenant_id",
    "trace_id",
    "kb_ids",
    "index_versions",
    "chunk_ids",
    "top_k",
    "latency_ms",
    "degraded_reason",
    "empty_reason",
    "created_at",
)
#: 改造后的列（十二列）：多出来的那一条就是映射列。**比集合不比顺序**——真库上的
#: ``ADD COLUMN`` 只能追加到物理列序末尾，而「它排在模型定义的哪一位」是 ORM 的事
#: （模型里它紧跟 ``index_versions``，见 ``tests/test_retrieval_trace_doc_versions.py``）。
AFTER_COLUMNS = BEFORE_COLUMNS + ("doc_versions",)

#: 改造前那两条索引；本票不加也不删索引。
EXISTING_INDEXES = frozenset({"ix_retrieval_traces_tenant_id", "ix_retrieval_traces_trace_id"})

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """有状态的假 ``op``：维护「库里现在有哪些列 / 索引」，别的一律不接受。"""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {
            "retrieval_traces": {
                name: {
                    "type": "prior",
                    "nullable": name in ("index_versions", "degraded_reason", "empty_reason"),
                    "default": None,
                }
                for name in BEFORE_COLUMNS
            }
        }
        self.indexes: dict[str, dict[str, Any]] = {
            name: {"table": "retrieval_traces", "columns": ("tenant_id",), "unique": False}
            for name in EXISTING_INDEXES
        }
        self.executed: list[str] = []

    # ── 本迁移用到的两个 op ──

    def add_column(self, table_name: str, column: Any) -> None:
        assert table_name in self.tables, table_name
        assert column.name not in self.tables[table_name], f"{column.name} 已经在了"
        default = getattr(column, "server_default", None)
        self.tables[table_name][column.name] = {
            "type": str(column.type),
            "nullable": bool(column.nullable),
            "default": None if default is None else str(default),
        }

    def drop_column(self, table_name: str, column_name: str, **_kwargs: Any) -> None:
        assert table_name in self.tables, table_name
        assert column_name in self.tables[table_name], f"{table_name}.{column_name} 不存在"
        self.tables[table_name].pop(column_name)

    def execute(self, statement: Any) -> None:  # pragma: no cover - 本迁移一句 DML 都不发
        self.executed.append(str(statement))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}")

    # ── 断言用的便利读法 ──

    def columns_of(self, table_name: str) -> tuple[str, ...]:
        return tuple(self.tables[table_name])

    def snapshot(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        return {name: dict(spec) for name, spec in self.tables.items()}, dict(self.indexes)


def _migration_path(slug: str = SLUG) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t179_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _revision_of(path: Path) -> str:
    match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 revision"
    return match.group("revision")


def _down_revision_of(path: Path) -> str | None:
    match = _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 down_revision"
    value = match.group("down_revision").strip()
    if value.startswith("None"):
        return None
    found = re.search(r"""['"]([^'"]+)['"]""", value)
    assert found is not None, f"{path.name}: 看不懂 down_revision={value}"
    return found.group(1)


def _migrate(direction: str) -> _RecordingOp:
    """把本票的迁移跑一遍；``downgrade`` 总是**先升后降**（真实用法就是如此）。"""

    recorder = _RecordingOp()
    module = _load_migration(_migration_path())
    module.op = recorder
    module.upgrade()
    assert set(recorder.columns_of("retrieval_traces")) == set(AFTER_COLUMNS), "先升：形状得先对"
    if direction == "downgrade":
        recorder.executed.clear()
        module.downgrade()
    return recorder


# ──────────────────────────────────────────────────────── 命名、链与 head 唯一性


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path()
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_chains_onto_the_previous_head_and_stays_in_the_single_chain() -> None:
    """接在 #172 的版本表后面，且仍是**唯一**一条链的链尾。"""

    path = _migration_path()
    assert _down_revision_of(path) == PREVIOUS_HEAD

    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(candidate): candidate for candidate in paths}
    down_revisions = {_down_revision_of(candidate) for candidate in paths} - {None}
    heads = set(revisions) - down_revisions

    assert len(heads) == 1, f"迁移链必须只有一条，实际 head={sorted(heads)}"
    # 链尾后来前移过（第 13 步 T1 的 add_retrieval_authorization 接在本票之后），
    # 所以「本票是链尾」这半句不再成立；改守「本票仍在唯一一条链上」。
    assert _revision_of(path) in revisions, "本票的迁移应当仍在链上"

    walked: list[str] = []
    cursor: str | None = next(iter(heads))
    while cursor is not None:
        walked.append(cursor)
        parent = _down_revision_of(revisions[cursor])
        if parent is not None:
            assert parent in revisions, f"{parent}: down_revision 指向一条不存在的迁移"
        cursor = parent

    assert PREVIOUS_HEAD in walked, "旧 head 不在链上"


# ────────────────────────────────────────────────────────────────── upgrade


def test_upgrade_adds_exactly_one_mapping_column_next_to_the_index_versions_one() -> None:
    recorder = _migrate("upgrade")

    assert set(recorder.columns_of("retrieval_traces")) == set(AFTER_COLUMNS)
    assert len(AFTER_COLUMNS) == len(BEFORE_COLUMNS) + 1, "只加一列，不是加一簇"


def test_the_model_puts_the_two_mapping_columns_side_by_side() -> None:
    """模型里的顺序是给读代码的人看的：两个映射列挨着（形状同源的东西放一起）。"""

    keys = [column.key for column in RetrievalTrace.__table__.columns]

    assert keys.index("doc_versions") == keys.index("index_versions") + 1


def test_the_new_column_is_a_nullable_jsonb_mapping_without_a_default() -> None:
    """可空（老行没有稿号可记）、JSONB（映射，不是两个数组）、**没有** server_default。"""

    spec = _migrate("upgrade").tables["retrieval_traces"]["doc_versions"]

    assert spec["type"] == "JSONB"
    assert spec["nullable"] is True
    assert spec["default"] is None, (
        "给默认值会让「这一行是老的」与「这次没命中任何文档」变成同一个读数"
    )


def test_upgrade_leaves_the_other_columns_and_the_indexes_alone() -> None:
    before, before_indexes = _RecordingOp().snapshot()
    recorder = _migrate("upgrade")
    after, after_indexes = recorder.snapshot()

    assert after_indexes == before_indexes, "这一列不是检索条件：不加索引"
    assert recorder.executed == [], "只加列，一句 DML 都不发（不回填）"
    for name, spec in before["retrieval_traces"].items():
        assert after["retrieval_traces"][name] == spec, f"{name} 不该被动过"


# ────────────────────────────────────────────────────────────────── downgrade


def test_downgrade_removes_the_column_and_leaves_the_previous_shape_exactly() -> None:
    """降级就是放弃「记得哪几稿」这个能力：列删掉，其余十一列逐项等于改造前。"""

    recorder = _migrate("downgrade")

    assert set(recorder.columns_of("retrieval_traces")) == set(BEFORE_COLUMNS)
    assert not [
        name for name, spec in recorder.indexes.items() if spec["table"] != "retrieval_traces"
    ]
    assert set(recorder.indexes) == EXISTING_INDEXES, "降级不该顺手动索引"
