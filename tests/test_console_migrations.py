"""票 #80：两条控制面迁移（sessions / audit_logs）。

与身份五表那组测试同一个口径，三条都不需要真数据库：

1. **一表一迁移、接在既有链尾**：整条链仍然只有一个 head，且就是本票的第二条；
2. **可升可降且对称**：把 ``op`` 换成记录器跑一遍，建了什么就必须删掉什么；
3. **只建新表**：不碰既有业务表，也不往任何表里灌数据。
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

# 本票的两条迁移，按建表顺序。左边是文件名里的 slug，右边是它建的表。
NEW_MIGRATIONS = (
    ("create_sessions", "sessions"),
    ("create_audit_logs", "audit_logs"),
)

#: 本票之前那条 head（``create api_keys``）。
PREVIOUS_HEAD = "bd5a6e3b8fa4"

#: 本票之前就已经存在的表：这两条迁移一个字都不许动它们。
#: ``retrieval_traces`` 在 #101 由 ``retrieval_logs`` 改名（并只留结构），清单跟着改名。
EXISTING_TABLES = (
    "knowledge_bases",
    "documents",
    "chunks",
    "retrieval_traces",
    "tenants",
    "plans",
    "accounts",
    "key_policies",
    "api_keys",
)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """假 ``op``：只记录建了什么、删了什么，别的一律不接受。

    两条迁移都应该是「纯建表」，所以 ``execute`` / ``add_column`` 这类都写成直接失败：
    真有人往里塞数据回填，这条测试会先炸，而不是等到某台机器上才出事。
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.dropped: list[str] = []
        self.tables: dict[str, tuple[Any, ...]] = {}

    def f(self, name: str) -> str:
        return name

    def create_table(self, table_name: str, *args: Any, **_kwargs: Any) -> None:
        self.created.append(f"table:{table_name}")
        self.tables[table_name] = args

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        self.dropped.append(f"table:{table_name}")

    def create_index(
        self, index_name: str, table_name: str, _columns: Any, **kwargs: Any
    ) -> None:
        self.created.append(f"index:{table_name}.{index_name}")

    def drop_index(self, index_name: str, table_name: str | None = None, **_kwargs: Any) -> None:
        self.dropped.append(f"index:{table_name}.{index_name}")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本次迁移不该用到 op.{name}：两条迁移只建表与索引")


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t80_migration_{path.stem}", path)
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


def _record(path: Path) -> tuple[_RecordingOp, ModuleType]:
    module = _load_migration(path)
    recorder = _RecordingOp()
    module.op = recorder
    return recorder, module


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


# ─────────────────────────────────────────────────────────── 命名、链与 head 唯一性


def test_each_new_migration_is_named_after_its_twelve_hex_revision() -> None:
    for slug, _table in NEW_MIGRATIONS:
        path = _migration_path(slug)
        revision = _revision_of(path)
        assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
        assert path.name == f"{revision}_{slug}.py"


def test_the_whole_chain_still_has_exactly_one_head_and_it_is_the_profile_presets_table() -> None:
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(path): path for path in paths}
    down_revisions = {_down_revision_of(path) for path in paths} - {None}

    heads = set(revisions) - down_revisions
    assert len(heads) == 1, f"迁移链必须只有一个 head，实际 {sorted(heads)}"
    # #93 追加了 runtime_settings、#94 追加了 model_configs，档位预设（profile_presets）
    # 又接在它们后面，所以链尾继续前移。这条断言每加一条迁移就要改一次——这是刻意的：
    # 「链尾是哪个文件」必须是人看过一眼才确认的事，不该由代码自动推导出来。
    # #101 的归属索引改名（``rename_to_retrieval_traces``）再接在档位预设之后；
    # #105 的反馈表（``create_retrieval_feedback``）又接在改名之后。
    assert heads == {_revision_of(_migration_path("create_retrieval_feedback"))}


def test_the_new_migrations_chain_onto_the_previous_head_in_table_order() -> None:
    expected_down = PREVIOUS_HEAD
    for slug, _table in NEW_MIGRATIONS:
        path = _migration_path(slug)
        assert _down_revision_of(path) == expected_down, f"{path.name}: 链接不到上一条"
        expected_down = _revision_of(path)


# ─────────────────────────────────────────────────── 一表一迁移、可升可降且对称


@pytest.mark.parametrize(("slug", "table"), NEW_MIGRATIONS, ids=[slug for slug, _ in NEW_MIGRATIONS])
def test_one_migration_creates_exactly_one_table(slug: str, table: str) -> None:
    recorder, module = _record(_migration_path(slug))

    module.upgrade()

    created_tables = [item for item in recorder.created if item.startswith("table:")]
    assert created_tables == [f"table:{table}"]


@pytest.mark.parametrize(("slug", "table"), NEW_MIGRATIONS, ids=[slug for slug, _ in NEW_MIGRATIONS])
def test_every_created_object_is_dropped_again(slug: str, table: str) -> None:
    recorder, module = _record(_migration_path(slug))

    module.upgrade()
    created = sorted(recorder.created)
    module.downgrade()

    assert sorted(recorder.dropped) == created, "回滚之后库里不该留下任何本次新建的对象"
    assert f"table:{table}" in created


@pytest.mark.parametrize(("slug", "table"), NEW_MIGRATIONS, ids=[slug for slug, _ in NEW_MIGRATIONS])
def test_new_tables_carry_no_foreign_keys(slug: str, table: str) -> None:
    recorder, module = _record(_migration_path(slug))

    module.upgrade()

    columns = [arg for arg in recorder.tables[table] if isinstance(arg, sa.Column)]
    assert columns, "建表语句里必须有列"
    assert not [column for column in columns if column.foreign_keys]
    assert not any(isinstance(arg, sa.ForeignKeyConstraint) for arg in recorder.tables[table])


@pytest.mark.parametrize(("slug", "_table"), NEW_MIGRATIONS, ids=[slug for slug, _ in NEW_MIGRATIONS])
def test_new_migrations_never_touch_any_existing_table(slug: str, _table: str) -> None:
    recorder, module = _record(_migration_path(slug))

    module.upgrade()
    module.downgrade()

    touched = " ".join(recorder.created + recorder.dropped)
    for existing in EXISTING_TABLES:
        assert f":{existing}" not in touched, f"{slug}: 迁移碰到了 {existing}"
