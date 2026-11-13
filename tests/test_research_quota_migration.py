"""票 #150：Research 的两个独立配额列（``research_qps`` / ``research_daily``）的迁移。

这条迁移只做一件事：给 ``plans`` 与 ``key_policies`` 各加两列。三处断言：

1. **接在链尾**：``down_revision`` 指向本步此前的唯一 head，整条链仍然只有一个 head；
2. **存量行回填 0**：两列都 ``NOT NULL`` + ``server_default 0``——升级完的库与改造前的库
   跑出同一个结果（Research 仍然关着），这正是「0 等同于关闭」这条决定在迁移里的样子；
3. **可升可降**：回滚只删这四列，留下的是改造前那两张表。

种子值（pro 1/100、free 与 standard 0）不在这里断言——那是 ``PLAN_PRESETS`` 的事，
由 ``test_plan_presets.py`` 逐列守。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票接在它后面：#141 的索引构建版本表，本步此前的唯一 head。
PREVIOUS_HEAD = "e41b7c9d2a63"

#: 四列：两张表各两列，名字相同、含义按 ADR-0018 的两级各算一份。
EXPECTED_COLUMNS = (
    ("plans", "research_qps"),
    ("plans", "research_daily"),
    ("key_policies", "research_qps"),
    ("key_policies", "research_daily"),
)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """只记录加列 / 删列：本条迁移是「纯加列」，出现别的 op 就该先炸。"""

    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: Any) -> None:
        self.added.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str, **_kwargs: Any) -> None:
        self.dropped.append((table_name, column_name))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}：它只加列、只删列")


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t150_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _down_revision_of(path: Path) -> str | None:
    match = _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 down_revision"
    return match.group("down_revision").strip().strip("'\"")


def _revision_of(path: Path) -> str:
    match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 revision"
    return match.group("revision")


def test_the_research_quota_migration_sits_on_top_of_the_single_head() -> None:
    """整条链仍然只有一个 head，本票这条接在它之前的 head 上。

    按仓库惯例，加最后一条迁移的人改这一行：「链尾是哪个文件」要人看一眼确认。
    """

    revisions = {_revision_of(path): path for path in VERSIONS_DIR.glob("*.py")}
    pointed_at = {
        _down_revision_of(path)
        for path in VERSIONS_DIR.glob("*.py")
        if _down_revision_of(path) is not None
    }
    heads = sorted(set(revisions) - pointed_at)

    path = _migration_path("add_research_quotas")
    assert _down_revision_of(path) == PREVIOUS_HEAD, "本条迁移必须接在本步此前的唯一 head 上"
    assert len(heads) == 1, f"整条链应当只有一个 head，实际 {heads}"
    assert heads == [_revision_of(path)], f"链尾应当是本条迁移，实际 {heads}"


def test_the_migration_adds_four_not_null_columns_backfilled_with_zero() -> None:
    """四列、``NOT NULL``、``server_default 0``——存量行在升级那一刻拿到 0（= 关闭）。"""

    module = _load_migration(_migration_path("add_research_quotas"))
    recorder = _RecordingOp()

    module.op = recorder
    module.upgrade()

    assert [(table, column.name) for table, column in recorder.added] == list(EXPECTED_COLUMNS)
    for table, column in recorder.added:
        assert column.nullable is False, f"{table}.{column.name} 必须非空"
        assert str(column.server_default.arg) == "0", f"{table}.{column.name} 必须回填 0"
        assert column.type.python_type is int, f"{table}.{column.name} 必须是整数"


def test_the_migration_downgrade_drops_exactly_what_it_added() -> None:
    """可升可降：回滚只删这四列，留下的是「改造前那张表」。"""

    module = _load_migration(_migration_path("add_research_quotas"))
    recorder = _RecordingOp()

    module.op = recorder
    module.downgrade()

    assert recorder.added == []
    assert recorder.dropped == list(EXPECTED_COLUMNS)
