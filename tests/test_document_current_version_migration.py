"""#171 · 文档行的稿次列改名：``documents.doc_version`` → ``documents.current_version``。

不需要 PostgreSQL：在内存 SQLite 上按**既有三条迁移叠加后的形状**建出改造前的
``documents`` 表（``d657b09304ca`` 建表 + ``b71a4c5e2f90`` 加租约列 + ``4c7e8f9a0b1c``
加稿次列），跑一遍本票的迁移，于是「库变成了什么样」与「存量值还在不在」都是可以
查出来的事实，而不是把迁移文本再抄一遍。

三条要守的东西：

1. **改名，不是换列**：存量行的值跟着列走（7 还是 7），NOT NULL 与默认值 1 也都还在
   ——若名字改了而值没了，这一列就从「稿次指针」变成了「新列默认 1」；
2. **可升可降**：``downgrade()`` 跑完，列名、列集合、列序、值与改造前**逐项相等**；
3. **只动这一列**：``documents`` 其余列一个都不变（本票是纯改名，没有数据回填）。

``documents.doc_version`` 的值一律由第 1 稿解释，改名之后语义仍不变：它现在是
「当前稿指针」（见 GLOSSARY），后面才由稿次落地的票去写它。
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
from alembic.migration import MigrationContext
from alembic.operations import Operations


VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票的改名迁移（文件名里刻意不写旧列名：迁移清单里不该再出现它）。
SLUG = "rename_documents_current_version"
#: 改名之前那条 head（``drop orchestration switch``）。
PREVIOUS_HEAD = "f2b8d4a6c913"

#: 改造前 ``documents`` 的列，按库里真实的列序抄下来（见上面三条迁移）。
BEFORE_COLUMNS = (
    "id",
    "tenant_id",
    "kb_id",
    "title",
    "content",
    "source_type",
    "source_file_path",
    "source_filename",
    "status",
    "doc_version",
    "index_claimed_at",
    "error_message",
    "created_at",
    "updated_at",
)
#: 改造后：同一个位置上的那一列换了个名字，别的一列不多不少。
AFTER_COLUMNS = tuple(
    "current_version" if name == "doc_version" else name for name in BEFORE_COLUMNS
)

#: 存量行的值刻意不取 1：默认值也是 1，若迁移把它当新列重建，断言分不出来。
SEEDED_POINTER = 7

#: 改造前的表：initial 建表 + 租约列 + 稿次列，三条迁移叠出来的形状。
_BEFORE_DDL = """
create table documents (
    id varchar(36) not null primary key,
    tenant_id varchar(64) not null,
    kb_id varchar(36) not null,
    title varchar(512) not null,
    content text not null,
    source_type varchar(32) not null,
    source_file_path varchar(1024),
    source_filename varchar(512),
    status integer not null,
    doc_version integer not null default '1',
    index_claimed_at datetime,
    error_message text,
    created_at datetime not null,
    updated_at datetime not null
)
"""

_SEED_SQL = """
insert into documents
    (id, tenant_id, kb_id, title, content, source_type, status, doc_version, created_at, updated_at)
values
    ('doc-seeded', 'tenant_demo', 'kb-1', '售后手册', '退款流程见附件。', 'markdown', 3, :pointer,
     '2026-11-08 00:00:00', '2026-11-08 00:00:00')
"""

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


# ──────────────────────────────────────────────────────────── 迁移文件的装载


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t171_migration_{path.stem}", path)
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


# ──────────────────────────────────────────────────────────── 真库上的升降


def _before_change() -> sa.engine.Connection:
    """改造前的库：``documents`` 一张表，里面积着一行「第 7 稿」。"""

    connection = sa.create_engine("sqlite://").connect()
    connection.exec_driver_sql(_BEFORE_DDL)
    connection.execute(sa.text(_SEED_SQL), {"pointer": SEEDED_POINTER})
    assert _column_names(connection) == list(BEFORE_COLUMNS)
    return connection


def _migrate(direction: str) -> sa.engine.Connection:
    """把本票的迁移跑一遍；``downgrade`` 总是**先升后降**（真实用法就是如此）。

    只跑 ``downgrade()`` 而不先 ``upgrade()`` 的话，库里根本没有新列，那条断言测的
    就成了「往改造前的库里把旧列改回旧列」，而不是「退得回去」。
    """

    connection = _before_change()
    module = _load_migration(_migration_path(SLUG))
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        module.upgrade()
        if direction == "downgrade":
            module.downgrade()
    return connection


def _column_names(connection: sa.engine.Connection) -> list[str]:
    return [column["name"] for column in sa.inspect(connection).get_columns("documents")]


def _pointer_values(connection: sa.engine.Connection, column: str) -> list[int]:
    rows = connection.exec_driver_sql(
        f'select "{column}" from documents order by id'  # noqa: S608 - 列名来自本文件的常量
    ).fetchall()
    return [row[0] for row in rows]


# ──────────────────────────────────────────────────────── 命名、链与 head 唯一性


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path(SLUG)
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_chains_onto_the_previous_head_and_stays_in_the_single_chain() -> None:
    """本条迁移接在旧 head 之后，并且仍然待在**唯一一条链**上。

    不写「它的 revision 就是当前 head」：链尾会随后续迁移（稿次落地的票）继续前移，
    本条要守的性质是「接得上、不另起一头」，所以沿 ``down_revision`` 从当前 head
    往回走，确认本条的 revision 在路径上。
    """

    path = _migration_path(SLUG)
    assert _down_revision_of(path) == PREVIOUS_HEAD

    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(candidate): candidate for candidate in paths}
    down_revisions = {_down_revision_of(candidate) for candidate in paths} - {None}
    heads = set(revisions) - down_revisions

    assert len(heads) == 1, f"迁移链必须只有一条，实际 head={sorted(heads)}"

    walked: list[str] = []
    cursor: str | None = next(iter(heads))
    while cursor is not None:
        walked.append(cursor)
        parent = _down_revision_of(revisions[cursor])
        if parent is not None:
            assert parent in revisions, f"{parent}: down_revision 指向一条不存在的迁移"
        cursor = parent

    assert _revision_of(path) in walked, "本条迁移不在链上（说明它被孤立了）"
    assert PREVIOUS_HEAD in walked, "旧 head 不在链上"


# ───────────────────────────────────────────────────────────── upgrade：改名保数据


def test_upgrade_renames_the_column_in_place_and_keeps_the_column_order() -> None:
    """列换名字，位置不变：它还是同一列，不是删一列再加一列。"""

    connection = _migrate("upgrade")

    assert _column_names(connection) == list(AFTER_COLUMNS)


def test_upgrade_keeps_the_stored_value() -> None:
    """存量行的值跟着列走：改名不许把「第 7 稿」变成默认的第 1 稿。"""

    connection = _migrate("upgrade")

    assert _pointer_values(connection, "current_version") == [SEEDED_POINTER]


def test_upgrade_keeps_the_not_null_and_the_default() -> None:
    """NOT NULL 与默认值 1 都还挂在同一列上：新行不写这一列也拿得到 1。"""

    connection = _migrate("upgrade")

    column = {c["name"]: c for c in sa.inspect(connection).get_columns("documents")}[
        "current_version"
    ]
    assert column["nullable"] is False
    assert str(column["default"]) == "'1'"

    connection.exec_driver_sql(
        "insert into documents (id, tenant_id, kb_id, title, content, source_type, status,"
        " created_at, updated_at) values ('doc-fresh', 'tenant_demo', 'kb-1', '新文档', '',"
        " 'text', 0, '2026-11-15 00:00:00', '2026-11-15 00:00:00')"
    )
    assert _pointer_values(connection, "current_version") == [1, SEEDED_POINTER]


def test_upgrade_leaves_no_old_name_behind() -> None:
    """旧名字不许以任何形式留在库里（例如改名顺手多留一列）。"""

    connection = _migrate("upgrade")

    names = _column_names(connection)
    assert "doc_version" not in names
    assert sorted(names) == sorted(AFTER_COLUMNS)


# ───────────────────────────────────────────────────────────── downgrade：退得回去


def test_downgrade_restores_the_previous_shape_exactly() -> None:
    """回滚之后列名、列序与改造前逐项相等——不是「大致能跑」。"""

    connection = _migrate("downgrade")

    assert _column_names(connection) == list(BEFORE_COLUMNS)


def test_downgrade_keeps_the_stored_value() -> None:
    """回滚也不许丢值：退回去的仍是同一行的第 7 稿。"""

    connection = _migrate("downgrade")

    assert _pointer_values(connection, "doc_version") == [SEEDED_POINTER]


def test_downgrade_keeps_the_not_null_and_the_default() -> None:
    connection = _migrate("downgrade")

    column = {c["name"]: c for c in sa.inspect(connection).get_columns("documents")}["doc_version"]
    assert column["nullable"] is False
    assert str(column["default"]) == "'1'"


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_the_migration_never_touches_the_schema_beyond_that_one_column(
    direction: str,
) -> None:
    """纯改名：没有回填语句，也没有第二条 DDL 落在别的对象上。"""

    connection = _migrate(direction)

    expected_types = {
        "current_version" if name == "doc_version" else name: str(column["type"])
        for name, column in _before_change_columns().items()
    }
    actual_types = {
        column["name"]: str(column["type"])
        for column in sa.inspect(connection).get_columns("documents")
    }
    if direction == "downgrade":
        expected_types = {
            "doc_version" if name == "current_version" else name: value
            for name, value in expected_types.items()
        }

    assert actual_types == expected_types, "只换了名字：类型一个都不许变"


def _before_change_columns() -> dict[str, Any]:
    connection = _before_change()
    return {column["name"]: column for column in sa.inspect(connection).get_columns("documents")}
