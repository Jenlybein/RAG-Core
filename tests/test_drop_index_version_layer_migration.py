"""库级索引版本整层退场那条迁移（`e8b2c47d91f5`）：删的顺序、以及 downgrade 建回来的形状。

## 为什么需要单独的用例

这是本改造里唯一一条**不可回退**的迁移（`downgrade` 只重建结构、数据没有副本），而它删的是
`chunks` 这张稳态最大的表上的一列。两件事值得钉住：

1. **删的顺序**：`DROP INDEX` 必须在 `DROP COLUMN` 之前。反过来 PostgreSQL 会自己把索引一起
   删掉、迁移「成功」，但 `downgrade` 里重建索引的语句就与「先有列才有索引」错位——那条错位
   在 `--sql` 渲染里看不出来，只在真库上炸。
2. **`downgrade` 的保真度**：它建回来的表要能承接旧代码。列名/类型/可空/默认值错一个，
   「回滚」就变成一个跑不起来的假动作；而这次没有任何运行时用例会走 `downgrade`，只有这条
   用例守得住。

## 为什么用离线渲染而不是假 `op`

仓库里既有的迁移用例用一个有状态的假 `op`（`_RecordingOp`），那是为了验「迁移读了库里什么」。
这条迁移**不回读库**（纯 DDL），所以更准的做法是让 alembic 自己把语句渲染成 SQL——
那串 SQL 就是真库上会执行的东西，顺序与形状都在里面，没有第二层解释。
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

SLUG = "drop_index_version_layer"

#: 旧表建回来时**必须有**的 19 列（逐列对过上五次迁移的叠加结果）。
TABLE_COLUMNS = {
    "id",
    "tenant_id",
    "kb_id",
    "version",
    "status",
    "document_count",
    "chunk_count",
    "error_message",
    "created_at",
    "activated_at",
    "archived_at",
    "documents_planned",
    "doc_version",
    "documents_done",
    "converge_deadline",
    "failed_documents",
    "redispatch_count",
    "auto_activate",
    "params",
}

#: 只服务被删列的复合索引（`6dfebeded96c` 建的）。
CHUNK_VERSION_INDEX = "ix_chunks_tenant_id_kb_id_index_version"


def _load() -> ModuleType:
    matches = [p for p in VERSIONS_DIR.glob("*.py") if SLUG in p.name]
    assert len(matches) == 1, matches
    spec = importlib.util.spec_from_file_location("m_drop_index_version_layer", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(direction: str) -> str:
    module = _load()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://", opts={"as_sql": True, "output_buffer": buffer}
    )
    # 迁移模块顶部 `from alembic import op` 拿到的是一株「当前上下文」的代理；
    # 离线渲染要把它换成我们自己这个上下文里的 Operations，否则 op 没有上下文会抛错。
    module.op = Operations(context)  # type: ignore[attr-defined]
    getattr(module, direction)()
    return buffer.getvalue()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render("upgrade")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("downgrade")


def test_the_index_is_dropped_before_the_column_that_it_covers(upgrade_sql: str) -> None:
    """顺序反了在真库上会「看起来成功」、在 downgrade 上错位。"""

    index_at = upgrade_sql.find(f"DROP INDEX IF EXISTS {CHUNK_VERSION_INDEX}")
    column_at = upgrade_sql.find("DROP COLUMN IF EXISTS index_version")
    assert index_at != -1, "依赖列的索引必须先删"
    assert column_at != -1
    assert index_at < column_at, "DROP INDEX 必须排在 DROP COLUMN 之前"


def test_the_table_is_dropped_last(upgrade_sql: str) -> None:
    """表不引用别的表、也没有别的东西引用它；但它最后删，是为了让前三条先成功。"""

    table_at = upgrade_sql.find("DROP TABLE IF EXISTS index_build_versions")
    assert table_at != -1
    assert table_at > upgrade_sql.find("active_index_version"), "删表排在两次删列之后"


def test_lock_timeout_is_set_before_any_ddl(upgrade_sql: str) -> None:
    """`chunks` 是稳态最大的表：不设锁超时，排在一条慢查询后面的 DDL 会堵住所有写入。"""

    first_ddl = upgrade_sql.find("DROP INDEX")
    lock_at = upgrade_sql.find("SET lock_timeout")
    assert lock_at != -1, "必须有 lock_timeout"
    assert lock_at < first_ddl, "锁超时要在任何 DDL 之前设置"


def test_the_upgrade_touches_exactly_the_three_legacy_things(upgrade_sql: str) -> None:
    """只删三样：版本表、库的生效版本列、切片的索引版本列（外加那一条索引）。"""

    drops = [line for line in upgrade_sql.splitlines() if line.startswith(("DROP", "ALTER"))]
    assert [line.split("(")[0].strip() for line in drops] == [
        f"DROP INDEX IF EXISTS {CHUNK_VERSION_INDEX};",
        "ALTER TABLE chunks DROP COLUMN IF EXISTS index_version;",
        "ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS active_index_version;",
        "DROP TABLE IF EXISTS index_build_versions;",
    ], drops


def test_the_downgrade_brings_the_table_back_with_every_column(downgrade_sql: str) -> None:
    """回滚建回来的表要能承接旧代码：少一列，旧代码一读就崩。"""

    for column in TABLE_COLUMNS:
        assert column in downgrade_sql, f"回滚缺列：{column}"
    assert "uq_index_build_versions_tenant_kb_version" in downgrade_sql
    assert "ck_index_build_versions_status" in downgrade_sql


def test_the_downgrade_restores_the_two_columns_and_the_index(downgrade_sql: str) -> None:
    """两条列（与那条索引）也按原形状回来：`index_version` 不可空、默认 `v1`，生效版本可空。"""

    assert "ADD COLUMN index_version VARCHAR(32) DEFAULT 'v1' NOT NULL" in downgrade_sql
    assert "ADD COLUMN active_index_version VARCHAR(32)" in downgrade_sql
    assert CHUNK_VERSION_INDEX in downgrade_sql


def test_the_round_trip_is_symmetric(upgrade_sql: str) -> None:
    """`upgrade → downgrade → upgrade` 不留下任何结构差异：这里只核对「三样东西」都回来了。"""

    module = _load()
    assert module.down_revision == "b4e7c1a9d2f3", "必须接在归属索引那条迁移之后"
    # upgrade 删掉的三样，downgrade 里都能建回来——名字层面一一对上即可。
    for name in ("index_build_versions", "chunks", "knowledge_bases"):
        assert name in upgrade_sql and name in _render("downgrade"), name
