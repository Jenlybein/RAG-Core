"""票 #74：五张身份表的迁移。

三条要守的东西，都**不需要真数据库**：

1. **一表一迁移、接在既有链尾**：五条文件名带自己的 12 位 revision，整条链只有一个 head；
2. **可升可降且对称**：把 ``op`` 换成记录器跑一遍 ``upgrade()`` / ``downgrade()``，
   建了什么就必须删掉什么——漏删一项，回滚后库里就多一张没人管的表；
3. **不碰既有四张业务表**：这五条迁移只建新表，任何对业务表的 DDL 都算越界。

迁移是「只读文本 + 一个假 op」就能测完的东西，所以不必也不能连 PostgreSQL。
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

# 本票的五条迁移，按建表顺序。左边是文件名里的 slug，右边是它建的表。
NEW_MIGRATIONS = (
    ("create_tenants", "tenants"),
    ("create_plans", "plans"),
    ("create_accounts", "accounts"),
    ("create_key_policies", "key_policies"),
    ("create_api_keys", "api_keys"),
)

# 升级前就存在的四张业务表：这五条迁移一个字都不许动它们（不建、不改、不删、不写数据）。
EXISTING_TABLES = ("knowledge_bases", "documents", "chunks", "retrieval_logs")

PREVIOUS_HEAD = "cecd88875297"

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """假 ``op``：只记录建了什么、删了什么，别的一律不接受。

    五个迁移都应该是「纯建表」，所以这里把 ``execute`` / ``alter_column`` /
    ``add_column`` 都写成直接失败——真有人往迁移里塞数据回填，这条测试会先炸，
    而不是等到某台机器上才出事。
    """

    def __init__(self) -> None:
        self.created: list[str] = []
        self.dropped: list[str] = []
        self.tables: dict[str, tuple[Any, ...]] = {}
        self.unique_indexes: list[str] = []

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
        if kwargs.get("unique"):
            self.unique_indexes.append(f"{table_name}.{index_name}")

    def drop_index(self, index_name: str, table_name: str | None = None, **_kwargs: Any) -> None:
        self.dropped.append(f"index:{table_name}.{index_name}")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本次迁移不该用到 op.{name}：五条迁移只建表与索引")


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t74_migration_{path.stem}", path)
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


def test_the_whole_chain_has_exactly_one_head_and_it_is_the_last_table() -> None:
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(path): path for path in paths}
    down_revisions = {_down_revision_of(path) for path in paths} - {None}

    heads = set(revisions) - down_revisions
    assert len(heads) == 1, f"迁移链必须只有一个 head，实际 {sorted(heads)}"
    # #80 在 #74 的五条之后追加了 sessions / audit_logs；#93 再追加 runtime_settings，
    # 链尾因此继续前移到配置覆盖表。这里只守「唯一的 head 是链上最新那条」。
    assert heads == {_revision_of(_migration_path("create_runtime_settings"))}


def test_the_new_migrations_chain_onto_the_previous_head_in_table_order() -> None:
    """从上一版 head 出发，五条迁移按 租户 → 套餐 → 账号 → 配置 → Key 依次接上。"""

    expected_down = PREVIOUS_HEAD
    for slug, _table in NEW_MIGRATIONS:
        path = _migration_path(slug)
        assert _down_revision_of(path) == expected_down, f"{path.name}: 链接不到上一条"
        expected_down = _revision_of(path)

    assert expected_down == _revision_of(_migration_path("create_api_keys"))


# ─────────────────────────────────────────────────────── 一表一迁移、可升可降且对称


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
def test_new_migrations_never_touch_the_existing_business_tables(slug: str, _table: str) -> None:
    """迁移只建身份五表：既有四张业务表（含它们的数据）一个字都不动。"""

    recorder, module = _record(_migration_path(slug))

    module.upgrade()
    module.downgrade()

    touched = " ".join(recorder.created + recorder.dropped)
    for existing in EXISTING_TABLES:
        assert existing not in touched, f"{slug}: 迁移碰到了 {existing}"
