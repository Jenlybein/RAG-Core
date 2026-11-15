"""#172 · 稿次落地的迁移：建表、加列、回填、断言、清 JSONB 那份稿号。

不需要 PostgreSQL：在内存 SQLite 上按**本迁移之前**的真实形状建出 ``documents`` 与
``chunks``（``documents`` 的稿次列在 #171 已改名为 ``current_version``），播种几行，
把本票的迁移跑一遍。于是「库变成了什么样」「每篇文档是否有且只有一条 v1 版本行」
「校验和是不是那个入口算出来的」都是可以查出来的事实，而不是把迁移文本再抄一遍。

要守的东西：

1. **一稿一行**：文档数 == 版本行数，``(document_id, version)`` 唯一，版号取文档的
   当前稿指针（存量数据里恒为 1），正文、源文件与创建时间都从文档行拷过来；
2. **校验和只有一个入口**：回填行里的 sha256 与 ``app.utils.checksum`` 对**手写字面量**
   的期望值相等（Windows 换行与首尾空白都被磨掉），不是「迁移自己再算一遍」；
3. **断言只对「有切片的文档」硬停**：正文为空且有切片 → 迁移中止并给出可操作提示；
   没有切片的失败文档允许空正文；
4. **稿号只有一个来源**：``chunks`` 上多出真实的 ``doc_version`` 列（存量片按所属文档的
   指针回填），JSONB 里那份副本在 PostgreSQL 上被删掉——这条按方言分支，SQLite 上只断言
   结构部分（理由见迁移里的注释）；
5. **可升可降**：``downgrade()`` 之后 ``chunks`` 的列与索引、以及整张版本表都回到改造前。
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

#: 本票的迁移。
SLUG = "add_document_versions"
#: 本迁移接在 #171 的改名迁移后面。
PREVIOUS_HEAD = "5d8a2f1c7b34"

#: 改造前 ``chunks`` 的列（按库里真实的列序，见 ``d657b09304ca`` + ``6dfebeded96c``）。
BEFORE_CHUNK_COLUMNS = (
    "id",
    "tenant_id",
    "kb_id",
    "document_id",
    "content",
    "embedding",
    "metadata",
    "index_version",
)
#: 改造后：多一列稿号，别的原样。
AFTER_CHUNK_COLUMNS = BEFORE_CHUNK_COLUMNS + ("doc_version",)

#: 版本行按 D3 定下的列（与 ``app/models/document_version.py`` 同一份形状）。
EXPECTED_VERSION_COLUMNS = (
    "id",
    "document_id",
    "version",
    "content",
    "source_file_path",
    "checksum",
    "status",
    "created_at",
)

#: 手写字面量的 sha256（.NET SHA256 / UTF-8），与 ``tests/test_checksum.py`` 同一份来源。
SHA256_OF_REFUND_SENTENCE = (
    "24a0752703eb616c007ab44fcc4241c42e0ecdc0409a84767f9279da79807cd2"
)
SHA256_OF_EMPTY_TEXT = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

REFUND_SENTENCE = "退款流程见附件。"

#: 改造前的 ``documents``：#171 之后它叫 ``current_version``（非空、默认 1）。
_DOCUMENTS_DDL = """
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

#: 改造前的 ``chunks``：JSONB 在这里退化成 TEXT（离线套件不连 PostgreSQL）。
_CHUNKS_DDL = """
create table chunks (
    id varchar(36) not null primary key,
    tenant_id varchar(64) not null,
    kb_id varchar(36) not null,
    document_id varchar(36) not null,
    content text not null,
    embedding text not null,
    metadata text not null,
    index_version varchar(32) not null default 'v1'
)
"""

#: 三篇文档：一篇正常（有切片）、一篇解析失败且没有切片（正文允许为空）、一篇「第 3 稿」
#: 且正文是 Windows 换行 + 首尾空白（校验和必须与正常那篇相同）。
_GOOD_DOCUMENTS = """
insert into documents
    (id, tenant_id, kb_id, title, content, source_type, source_file_path, status,
     current_version, created_at, updated_at)
values
    ('doc-1', 'tenant_demo', 'kb-1', '售后手册', '退款流程见附件。', 'markdown',
     '/data/doc-1.md', 3, 1, '2026-11-08 00:00:00', '2026-11-08 00:00:00'),
    ('doc-2', 'tenant_demo', 'kb-1', '解析失败', '', 'text', null, 4, 1,
     '2026-11-08 00:00:00', '2026-11-08 00:00:00'),
    ('doc-3', 'tenant_demo', 'kb-1', 'Windows 稿', '退款流程见附件。' || char(13) || char(10) || '  ',
     'markdown', '/data/doc-3.md', 3, 3, '2026-11-01 00:00:00', '2026-11-08 00:00:00')
"""

_GOOD_CHUNKS = """
insert into chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, index_version)
values
    ('chunk-1', 'tenant_demo', 'kb-1', 'doc-1', '退款流程见附件。', '[]',
     '{"doc_version": 1, "chunk_type": "prose"}', 'v1'),
    ('chunk-2', 'tenant_demo', 'kb-1', 'doc-1', '附件。', '[]', '{"doc_version": 1}', 'v1'),
    ('chunk-3', 'tenant_demo', 'kb-1', 'doc-3', '退款流程见附件。', '[]',
     '{"doc_version": 3}', 'v1')
"""

_GOOD_SEED: tuple[str, ...] = (_GOOD_DOCUMENTS, _GOOD_CHUNKS)

#: 断言用例的种子：一篇好文档、一篇没有切片的空正文失败文档、一篇**有切片却空正文**的文档。
_BAD_DOCUMENTS = """
insert into documents
    (id, tenant_id, kb_id, title, content, source_type, status, current_version,
     created_at, updated_at)
values
    ('doc-good', 'tenant_demo', 'kb-1', '售后手册', '退款流程见附件。', 'markdown', 3, 1,
     '2026-11-08 00:00:00', '2026-11-08 00:00:00'),
    ('doc-failed', 'tenant_demo', 'kb-1', '解析失败', '', 'text', 4, 1,
     '2026-11-08 00:00:00', '2026-11-08 00:00:00'),
    ('doc-bad', 'tenant_demo', 'kb-1', '文件上传未回写', ' ' || char(13) || char(10), 'pdf', 3, 1,
     '2026-11-08 00:00:00', '2026-11-08 00:00:00')
"""

_BAD_CHUNKS = """
insert into chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata, index_version)
values
    ('chunk-good', 'tenant_demo', 'kb-1', 'doc-good', '退款流程见附件。', '[]', '{}', 'v1'),
    ('chunk-bad', 'tenant_demo', 'kb-1', 'doc-bad', '某一片', '[]', '{}', 'v1')
"""

_BAD_SEED: tuple[str, ...] = (_BAD_DOCUMENTS, _BAD_CHUNKS)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


# ──────────────────────────────────────────────────────────── 迁移文件的装载


def _migration_path(slug: str = SLUG) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t172_migration_{path.stem}", path)
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


# ─────────────────────────────────────────────────────────── 真库（SQLite）上的升降


def _before_change(seed: tuple[str, ...] = _GOOD_SEED) -> sa.engine.Connection:
    """改造前的库：``documents`` + ``chunks`` 两张表，里面积着 ``seed`` 里的几行。"""

    connection = sa.create_engine("sqlite://").connect()
    connection.exec_driver_sql(_DOCUMENTS_DDL)
    connection.exec_driver_sql(_CHUNKS_DDL)
    for statement in seed:
        connection.exec_driver_sql(statement)
    assert _chunk_columns(connection) == list(BEFORE_CHUNK_COLUMNS)
    return connection


def _migrate(
    direction: str, seed: tuple[str, ...] = _GOOD_SEED
) -> sa.engine.Connection:
    """把本票的迁移跑一遍；``downgrade`` 总是**先升后降**（真实用法就是如此）。"""

    connection = _before_change(seed)
    module = _load_migration(_migration_path())
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        module.upgrade()
        if direction == "downgrade":
            module.downgrade()
    return connection


def _chunk_columns(connection: sa.engine.Connection) -> list[str]:
    return [column["name"] for column in sa.inspect(connection).get_columns("chunks")]


def _indexes_of(connection: sa.engine.Connection, table: str) -> dict[str, tuple[str, ...]]:
    return {
        index["name"]: tuple(index["column_names"])
        for index in sa.inspect(connection).get_indexes(table)
    }


def _version_rows(connection: sa.engine.Connection) -> list[Any]:
    return connection.exec_driver_sql(
        "select id, document_id, version, content, source_file_path, checksum, status,"
        " created_at from document_versions order by document_id"
    ).fetchall()


def _chunk_doc_versions(connection: sa.engine.Connection) -> dict[str, int]:
    rows = connection.exec_driver_sql(
        "select id, doc_version from chunks order by id"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


# ──────────────────────────────────────────────────────── 命名、链与 head 唯一性


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path()
    revision = _revision_of(path)

    assert re.fullmatch(r"[0-9a-f]{12}", revision), f"{path.name}: revision 不是 12 位十六进制"
    assert path.name == f"{revision}_{SLUG}.py"


def test_it_chains_onto_the_previous_head_and_stays_in_the_single_chain() -> None:
    """接在 #171 的改名迁移后面，且仍是**唯一**一条链的链尾。"""

    path = _migration_path()
    assert _down_revision_of(path) == PREVIOUS_HEAD

    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(candidate): candidate for candidate in paths}
    down_revisions = {_down_revision_of(candidate) for candidate in paths} - {None}
    heads = set(revisions) - down_revisions

    assert len(heads) == 1, f"迁移链必须只有一条，实际 head={sorted(heads)}"
    assert _revision_of(path) in heads, "本票的迁移应当就是当前链尾"

    walked: list[str] = []
    cursor: str | None = next(iter(heads))
    while cursor is not None:
        walked.append(cursor)
        parent = _down_revision_of(revisions[cursor])
        if parent is not None:
            assert parent in revisions, f"{parent}: down_revision 指向一条不存在的迁移"
        cursor = parent

    assert PREVIOUS_HEAD in walked, "旧 head 不在链上"


# ────────────────────────────────────────────────────────────── upgrade：表与列


def test_upgrade_creates_the_versions_table_with_the_enumerated_columns() -> None:
    connection = _migrate("upgrade")

    columns = [
        column["name"]
        for column in sa.inspect(connection).get_columns("document_versions")
    ]
    assert columns == list(EXPECTED_VERSION_COLUMNS)


def test_upgrade_makes_the_document_and_version_pair_unique() -> None:
    connection = _migrate("upgrade")

    unique = {
        tuple(constraint["column_names"]): constraint["name"]
        for constraint in sa.inspect(connection).get_unique_constraints("document_versions")
    }
    assert tuple(sorted(unique)) == (("document_id", "version"),), (
        f"唯一约束必须恰好是 (document_id, version)，实际 {sorted(unique)}"
    )


def test_upgrade_indexes_the_document_id_column() -> None:
    connection = _migrate("upgrade")

    indexes = _indexes_of(connection, "document_versions")
    assert indexes["ix_document_versions_document_id"] == ("document_id",)


def test_upgrade_adds_no_foreign_key() -> None:
    """仓库零外键约定：文档删掉时版本行由删文档那条路径显式删（不是库级级联）。"""

    connection = _migrate("upgrade")

    assert sa.inspect(connection).get_foreign_keys("document_versions") == []


def test_upgrade_gives_every_chunk_a_real_doc_version_column() -> None:
    connection = _migrate("upgrade")

    assert _chunk_columns(connection) == list(AFTER_CHUNK_COLUMNS)
    column = {
        spec["name"]: spec for spec in sa.inspect(connection).get_columns("chunks")
    }["doc_version"]
    assert column["nullable"] is False
    assert str(column["default"]) == "'1'"


def test_upgrade_indexes_chunks_by_document_and_version() -> None:
    connection = _migrate("upgrade")

    indexes = _indexes_of(connection, "chunks")
    assert indexes["ix_chunks_document_id_doc_version"] == ("document_id", "doc_version")


def test_upgrade_leaves_the_documents_table_alone() -> None:
    """本迁移不碰 ``documents``：指针列是 #171 改的名，回填只读它。"""

    connection = _before_change()
    before = {
        column["name"]: str(column["type"])
        for column in sa.inspect(connection).get_columns("documents")
    }
    connection = _migrate("upgrade")

    after = {
        column["name"]: str(column["type"])
        for column in sa.inspect(connection).get_columns("documents")
    }
    assert after == before


# ──────────────────────────────────────────────────────── upgrade：回填一稿一行


def test_upgrade_backfills_exactly_one_version_row_per_document() -> None:
    connection = _migrate("upgrade")

    document_ids = [
        row[0]
        for row in connection.exec_driver_sql("select id from documents order by id").fetchall()
    ]
    rows = _version_rows(connection)

    assert [row[1] for row in rows] == document_ids, "一篇文档恰好一行，不许漏也不许多"
    assert len({row[0] for row in rows}) == len(rows), "版本行 id 必须各不相同"
    assert all(re.fullmatch(r"[0-9a-f]{32}", row[0]) for row in rows), (
        "版本行 id 由 (文档, 稿号) 确定性派生（重跑不会造出第二行）"
    )


def test_the_backfilled_row_is_the_documents_current_pointer() -> None:
    """版号取文档的当前稿指针；存量数据里它恒为 1，但迁移不许把它写死成 1。"""

    connection = _migrate("upgrade")

    assert {row[1]: row[2] for row in _version_rows(connection)} == {
        "doc-1": 1,
        "doc-2": 1,
        "doc-3": 3,
    }


def test_the_backfilled_row_carries_the_body_the_source_path_and_its_creation_time() -> None:
    """正文、源文件与创建时间都从文档行拷过来——存量那一稿建于文档行被创建的时刻。"""

    connection = _migrate("upgrade")

    rows = {row[1]: row for row in _version_rows(connection)}
    assert rows["doc-1"][3] == REFUND_SENTENCE, "正文必须是文档行上那份"
    assert rows["doc-1"][4] == "/data/doc-1.md"
    assert rows["doc-1"][5] == SHA256_OF_REFUND_SENTENCE
    assert rows["doc-1"][6] == "current", "回填出来的就是当前稿"
    assert rows["doc-1"][7] == "2026-11-08 00:00:00"
    assert rows["doc-3"][7] == "2026-11-01 00:00:00", "用文档行自己的创建时间，不是迁移时刻"


def test_the_backfilled_checksum_goes_through_the_single_normalization_entry() -> None:
    """Windows 换行与首尾空白被磨掉之后，两篇「同一份正文」算出同一个校验和。"""

    connection = _migrate("upgrade")

    checksums = {row[1]: row[5] for row in _version_rows(connection)}
    assert checksums["doc-1"] == SHA256_OF_REFUND_SENTENCE
    assert checksums["doc-3"] == SHA256_OF_REFUND_SENTENCE, (
        "CRLF 与尾随空白必须被 normalize_text 磨掉，否则判重从第一天就失效"
    )
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in checksums.values())


def test_a_failed_document_without_chunks_may_keep_an_empty_body() -> None:
    """没有切片的失败文档允许空正文：它还是得到一条内容为空、校验和稳定的版本行。"""

    connection = _migrate("upgrade")

    row = {row[1]: row for row in _version_rows(connection)}["doc-2"]
    assert row[3] == ""
    assert row[5] == SHA256_OF_EMPTY_TEXT


def test_existing_chunks_are_labelled_with_their_documents_pointer() -> None:
    """存量切片按所属文档的当前稿指针回填，于是「切片稿号 = 文档指针」当场成立。"""

    connection = _migrate("upgrade")

    assert _chunk_doc_versions(connection) == {"chunk-1": 1, "chunk-2": 1, "chunk-3": 3}


# ────────────────────────────────────────────────── upgrade：空正文的硬停与提示


def test_upgrade_stops_hard_when_a_chunked_document_has_no_body() -> None:
    """有切片却没有正文 = 迁移一跑就会写出一条「空正文」版本行，宁可中止。"""

    connection = _before_change(_BAD_SEED)
    module = _load_migration(_migration_path())
    context = MigrationContext.configure(connection)

    with Operations.context(context), pytest.raises(RuntimeError) as caught:
        module.upgrade()

    message = str(caught.value)
    assert "doc-bad" in message, "提示里要点名是哪篇（至少给一个样例）"
    assert "doc-failed" not in message, "没有切片的失败文档不该被算进来"
    assert "scripts/backfill_document_content.py" in message, "提示必须是可操作的"
    assert "--apply" in message, "提示要写清怎么真正补齐（默认 dry-run 不写）"


def test_the_hard_stop_speaks_raise_exception_on_postgresql() -> None:
    """真库上这一停是 ``RAISE EXCEPTION``（库级），不是只靠 Python 抛。"""

    module = _load_migration(_migration_path())
    recorder = _RecordingOp()
    module.op = recorder

    with pytest.raises(RuntimeError):
        module._stop_on_empty_content(_FakeBind("postgresql"), ["doc-bad"])

    assert len(recorder.executed) == 1
    sql = recorder.executed[0]
    assert sql.startswith("DO $$")
    assert "RAISE EXCEPTION" in sql, sql
    assert "scripts/backfill_document_content.py" in sql, "库级提示也要可操作"
    assert "doc-bad" in sql


# ─────────────────────────────────────── JSONB 里那份稿号副本：按方言分支


def test_the_jsonb_copy_of_the_version_is_dropped_on_postgresql() -> None:
    """PostgreSQL 上把 ``chunks.metadata - 'doc_version'`` 一次性删掉。"""

    module = _load_migration(_migration_path())
    recorder = _RecordingOp()
    module.op = recorder

    module._drop_meta_doc_version(_FakeBind("postgresql"))

    assert len(recorder.executed) == 1
    assert "metadata = metadata - 'doc_version'" in recorder.executed[0]
    assert "chunks" in recorder.executed[0]


def test_the_jsonb_copy_is_left_alone_on_other_dialects() -> None:
    """SQLite 没有 ``jsonb - text``：结构测试里这一步跳过（迁移里按方言分支）。"""

    module = _load_migration(_migration_path())
    recorder = _RecordingOp()
    module.op = recorder

    module._drop_meta_doc_version(_FakeBind("sqlite"))

    assert recorder.executed == []


def test_downgrade_puts_the_jsonb_copy_back_from_the_column() -> None:
    """回滚要对称：那份副本重新由**列**（唯一来源）生出来，而不是凭空消失。"""

    module = _load_migration(_migration_path())
    recorder = _RecordingOp()
    module.op = recorder

    module._restore_meta_doc_version(_FakeBind("postgresql"))

    assert len(recorder.executed) == 1
    assert "jsonb_build_object('doc_version', doc_version)" in recorder.executed[0]
    assert "chunks" in recorder.executed[0]

    recorder.executed.clear()
    module._restore_meta_doc_version(_FakeBind("sqlite"))
    assert recorder.executed == []


def test_offline_rendering_is_refused_with_a_reason() -> None:
    """``--sql`` 渲染时没有库可读，而校验和必须逐行走 Python 入口（不许用 digest）。"""

    module = _load_migration(_migration_path())
    connection = _before_change()
    context = MigrationContext.configure(connection, opts={"as_sql": True})

    with Operations.context(context), pytest.raises(RuntimeError) as caught:
        module.upgrade()

    message = str(caught.value)
    assert "--sql" in message
    assert "checksum" in message.lower() or "校验和" in message


# ───────────────────────────────────────────────────────────── downgrade：退得回去


def test_downgrade_drops_the_versions_table() -> None:
    connection = _migrate("downgrade")

    assert "document_versions" not in sa.inspect(connection).get_table_names()


def test_downgrade_restores_the_previous_chunks_shape() -> None:
    connection = _migrate("downgrade")

    assert _chunk_columns(connection) == list(BEFORE_CHUNK_COLUMNS)
    assert "ix_chunks_document_id_doc_version" not in _indexes_of(connection, "chunks")


def test_downgrade_keeps_the_documents_table_and_its_pointer_values() -> None:
    connection = _migrate("downgrade")

    columns = [column["name"] for column in sa.inspect(connection).get_columns("documents")]
    assert "current_version" in columns and "doc_version" not in columns

    pointers = connection.exec_driver_sql(
        "select id, current_version from documents order by id"
    ).fetchall()
    assert [tuple(row) for row in pointers] == [("doc-1", 1), ("doc-2", 1), ("doc-3", 3)]


# ────────────────────────────────────────── 守门条件：迁移不改变任何一行的可见性


#: 迁移前后都要给出同样答案的读语句——它们是**今天**检索链路真实发出的那几种形状：
#: 范围检索（租户 + 库 + 索引版本）、文档列表（按状态）、以及「这个库有多少片」。
#: 迁移只加列 / 建表 / 补版本行，一行都不该多、也不该少。
_GATE_QUERIES = (
    "select count(*) from documents",
    "select count(*) from documents where tenant_id = 'tenant_demo' and status = 3",
    "select count(*) from chunks",
    "select count(*) from chunks where tenant_id = 'tenant_demo' and kb_id = 'kb-1'"
    " and index_version = 'v1'",
    "select document_id, count(*) from chunks group by document_id order by document_id",
)


def test_the_migration_changes_no_rows_visible_to_the_existing_queries() -> None:
    """守门条件的离线替代：迁移前后同一组读语句给出**完全相同**的结果。

    真库上的守门（真实容器、真实 pgvector、真实 query 条数）不在这条离线用例的能力范围
    内——它连不上 PostgreSQL。这里守的是同一件事在数据层面的那一半：迁移不做任何
    ``DELETE`` / 不改变任何一行的归属，于是「条数变少」只能来自代码而不是数据。
    """

    connection = _before_change()
    before = [connection.exec_driver_sql(query).fetchall() for query in _GATE_QUERIES]

    module = _load_migration(_migration_path())
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        module.upgrade()

    after = [connection.exec_driver_sql(query).fetchall() for query in _GATE_QUERIES]

    assert after == before, "迁移不许改变任何一行的可见性（条数或分组）"
    assert all(result for result in before), (
        "门禁语句自己得先返回点什么，否则这条用例什么都没验"
    )


# ────────────────────────────────────────────────────────────────────── 测试替身


class _RecordingOp:
    """只记下 ``op.execute(...)`` 发出的 SQL（迁移里的两句方言分支用得上）。"""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> None:
        self.executed.append(str(statement))


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    """``op.get_bind()`` 的替身：迁移只读它的 ``dialect.name``。"""

    def __init__(self, dialect_name: str) -> None:
        self.dialect = _FakeDialect(dialect_name)
