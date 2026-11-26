"""#217 · 文档状态拆出「排队中」：数据迁移的离线验收（不需要 PostgreSQL）。

与 #171 的迁移用例同一形状：在内存 SQLite 上按**改造前的形状**建出 ``documents`` 表、
种下各种状态的行，然后把这个数据迁移跑一遍——「哪些行变成了 4、哪些一个字都没动、
回滚之后退回什么」都是能查出来的事实，而不是把 SQL 再抄一遍。

三条要守的东西：

1. **只动 ``3`` 且租约为空**：租约有效的那一行是真的有人在跑，把它写成排队中是**撒谎**，
   而且会让界面在有人正在写切片时允许删除；
2. **可回滚**：反向迁移把所有 ``4`` 退回 ``3``（租约仍为空，与改造前的形状逐字一致）；
3. **接得上、不另起一头**：本条迁移接在旧 head 上，整条链仍然只有一头。

为什么不在这条缝上查「迁移之后 Worker 抢不抢得到」：那是 CAS 的语义，归真库用例
（``tests/test_integration_queue_state.py``）。这里只问「库里那几行变成什么样」。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.utils.status import DocumentStatus

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票的迁移（文件名里只写主题，不写具体编号——编号在文件里）。
SLUG = "queue_document_status"
#: 接在谁后面（``f8b3c2d91a70`` 是改造前那条 head）。
PREVIOUS_HEAD = "b2c7d4e91a35"

#: 改造前的表：与当前模型逐列一致（本票只改数据，不动结构）。
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
    current_version integer not null default '1',
    index_claimed_at datetime,
    error_message text,
    created_at datetime not null,
    updated_at datetime not null
)
"""

#: 五行的语义各不同：迁移只该改第一行。
_SEED_SQL = """
insert into documents
    (id, tenant_id, kb_id, title, content, source_type, status, index_claimed_at,
     created_at, updated_at)
values
    ('doc-legacy-queued', 'tenant_demo', 'kb-1', '排了队没人接', '', 'markdown', 3, null,
     '2026-11-20 00:00:00', '2026-11-20 00:00:00'),
    ('doc-running', 'tenant_demo', 'kb-1', '正在跑', '', 'markdown', 3, '2026-11-26 00:00:00',
     '2026-11-20 00:00:01', '2026-11-26 00:00:00'),
    ('doc-queued', 'tenant_demo', 'kb-1', '已经是排队中', '', 'markdown', 4, null,
     '2026-11-20 00:00:02', '2026-11-20 00:00:02'),
    ('doc-success', 'tenant_demo', 'kb-1', '成功', '', 'markdown', 1, null,
     '2026-11-20 00:00:03', '2026-11-20 00:00:03'),
    ('doc-failed', 'tenant_demo', 'kb-1', '失败', '', 'markdown', 2, null,
     '2026-11-20 00:00:04', '2026-11-20 00:00:04')
"""

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t217_migration_{path.stem}", path)
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


def _migrate(direction: str) -> sa.engine.Connection:
    """把这条迁移跑一遍；``downgrade`` 总是**先升后降**（真实用法就是如此）。"""

    connection = sa.create_engine("sqlite://").connect()
    connection.exec_driver_sql(_BEFORE_DDL)
    connection.exec_driver_sql(_SEED_SQL)
    module = _load_migration(_migration_path(SLUG))
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        module.upgrade()
        if direction == "downgrade":
            module.downgrade()
    return connection


def _statuses(connection: sa.engine.Connection) -> dict[str, int]:
    rows = connection.exec_driver_sql("select id, status from documents order by id").fetchall()
    return {row[0]: row[1] for row in rows}


# ─────────────────────────────────────────────────────────── 命名、链与 head 唯一性


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path(SLUG)
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_chains_onto_the_previous_head_and_keeps_a_single_head() -> None:
    """接得上、不另起一头：沿 ``down_revision`` 从当前 head 走回根，本条在路径上。"""

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


# ───────────────────────────────────────────────────────────── upgrade：只动那一类行


def test_upgrade_turns_only_legacy_processing_rows_without_a_lease_into_queued() -> None:
    """只动 ``3`` 且租约为空：在跑的那一行、以及三个别的状态一行都不许改。"""

    assert _statuses(_migrate("upgrade")) == {
        "doc-legacy-queued": DocumentStatus.QUEUED,
        # 租约有效 = 真的有人在跑。把它写成排队中会让界面在有人写切片时允许删除。
        "doc-running": DocumentStatus.PROCESSING,
        "doc-queued": DocumentStatus.QUEUED,
        "doc-success": DocumentStatus.SUCCESS,
        "doc-failed": DocumentStatus.FAILED,
    }


def test_upgrade_is_idempotent() -> None:
    """再跑一遍什么都不变（清扫器 / 运维脚本可能重复触发同一条语句）。"""

    connection = _migrate("upgrade")
    module = _load_migration(_migration_path(SLUG))
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        module.upgrade()

    assert _statuses(connection) == {
        "doc-legacy-queued": DocumentStatus.QUEUED,
        "doc-running": DocumentStatus.PROCESSING,
        "doc-queued": DocumentStatus.QUEUED,
        "doc-success": DocumentStatus.SUCCESS,
        "doc-failed": DocumentStatus.FAILED,
    }


# ──────────────────────────────────────────────────────────── downgrade：退得回去


def test_downgrade_puts_every_queued_row_back_to_processing() -> None:
    """回滚把 4 退回 3（租约仍为空）：改造前的三态读侧照样把这些行当「处理中」。"""

    assert _statuses(_migrate("downgrade")) == {
        "doc-legacy-queued": DocumentStatus.PROCESSING,
        "doc-running": DocumentStatus.PROCESSING,
        "doc-queued": DocumentStatus.PROCESSING,
        "doc-success": DocumentStatus.SUCCESS,
        "doc-failed": DocumentStatus.FAILED,
    }


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_the_migration_never_touches_the_schema(direction: str) -> None:
    """纯数据迁移：列集合与列序逐项不变，一行 DDL 都没有。"""

    connection = _migrate(direction)
    columns = [
        column["name"] for column in sa.inspect(connection).get_columns("documents")
    ]

    assert columns == [
        "id",
        "tenant_id",
        "kb_id",
        "title",
        "content",
        "source_type",
        "source_file_path",
        "source_filename",
        "status",
        "current_version",
        "index_claimed_at",
        "error_message",
        "created_at",
        "updated_at",
    ]
