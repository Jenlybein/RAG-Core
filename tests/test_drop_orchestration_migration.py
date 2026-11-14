"""#138 收窄：删掉编排开关那三列的迁移（`add_orchestration_switch` 的逆操作）。

撤掉一个能力时，**库里的列也必须一起走**：留着它，下一个人会以为这个能力只是被关掉了，
而「有一列、没人读」正是最容易让一个死能力重新长回来的土壤（ADR-0029「被否决的做法」
最后一行就是这个理由）。

这条迁移是纯删列：``upgrade`` 删三列，``downgrade`` 重建三列并**回填 ``false``**——
回滚之后得到的是「装好开关、但一个都不开」的那张库，也就是加列那条迁移跑完的样子。

命名与接法沿用仓库惯例：12 位十六进制 revision、接在**当时的唯一 head** 上。这里把
「当时」写成常量：链尾后来前移过（#152 / #141 / #150）。本票的接点也能被更晚的迁移接上，
所以「本票是链尾」那半句不再成立，见
``tests/test_section_edge_table.py`` 里那条改写成「在链上、不另起一头」的守卫。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

SLUG = "drop_orchestration_switch"

#: 本票接在它后面（#152 的关系边表，本步此前的唯一 head）。
PREVIOUS_HEAD = "b3d7e1a95c24"

#: 迁移要删的三列：``(表, 列)``。与 ``add_orchestration_switch`` 加的那三列逐字对应。
EXPECTED_COLUMNS = (
    ("plans", "orchestration_allowed"),
    ("key_policies", "orchestration_allowed"),
    ("profile_presets", "orchestration"),
)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """只记录加列 / 删列：本条迁移是「纯删列」，出现别的 op 就该先炸。"""

    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: Any) -> None:
        self.added.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str, **_kwargs: Any) -> None:
        self.dropped.append((table_name, column_name))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}：它只删列、只加列")


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t138_migration_{path.stem}", path)
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
    return match.group("down_revision").strip().strip("'\"")


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path(SLUG)
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_sits_on_the_head_it_was_written_against_and_is_the_single_head() -> None:
    """接在当时的唯一 head 上，且整条链仍然只有一个 head。

    「链尾是哪个文件」必须是人看过一眼确认的事，不由代码自动推导出来——所以这里写死
    ``PREVIOUS_HEAD``，每加一条迁移就要人改一次。
    """

    path = _migration_path(SLUG)
    assert _down_revision_of(path) == PREVIOUS_HEAD, "本条迁移必须接在本步此前的唯一 head 上"

    revisions = {_revision_of(item): item for item in VERSIONS_DIR.glob("*.py")}
    pointed_at = {
        _down_revision_of(item) for item in VERSIONS_DIR.glob("*.py") if _down_revision_of(item)
    }
    heads = sorted(set(revisions) - pointed_at)

    assert len(heads) == 1, f"整条链应当只有一个 head，实际 {heads}"
    assert heads == [_revision_of(path)], f"链尾应当是本条迁移，实际 {heads}"


def test_upgrade_drops_exactly_the_three_columns() -> None:
    """升级只删这三列，别的什么都不动。"""

    module = _load_migration(_migration_path(SLUG))
    recorder = _RecordingOp()

    module.op = recorder
    module.upgrade()

    assert recorder.added == []
    assert recorder.dropped == list(EXPECTED_COLUMNS)


def test_downgrade_rebuilds_the_three_columns_with_a_false_backfill() -> None:
    """回滚重建三列：``NOT NULL`` + ``server_default false``。

    回填 ``false`` 是**语义**要求，不是形式要求：回滚后得到的是「开关装好了、一个都没开」
    的那张库。若回填成 ``true``，回滚就等于悄悄把能力打开了。
    """

    module = _load_migration(_migration_path(SLUG))
    recorder = _RecordingOp()

    module.op = recorder
    module.downgrade()

    assert [(table, column.name) for table, column in recorder.added] == list(EXPECTED_COLUMNS)
    assert recorder.dropped == []
    for table, column in recorder.added:
        assert column.nullable is False, f"{table}.{column.name} 必须非空"
        assert "false" in str(column.server_default.arg).lower(), (
            f"{table}.{column.name} 必须回填 False"
        )
        assert column.type.python_type is bool


def test_the_two_migrations_are_exact_inverses_of_each_other() -> None:
    """加列那条与删列这条互为逆操作：同一批 ``(表, 列)``，方向相反。

    分成两条迁移而不是改历史：``add_orchestration_switch`` 已经在别人的库上跑过了，
    改它等于让「同一个 revision id 在不同机器上是两件事」——那比留着一条删列迁移糟得多。
    """

    add = _load_migration(_migration_path("add_orchestration_switch"))
    drop = _load_migration(_migration_path(SLUG))

    assert tuple(add._COLUMNS) == tuple(drop._COLUMNS) == EXPECTED_COLUMNS
