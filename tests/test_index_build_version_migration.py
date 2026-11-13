"""#141 · 索引构建版本的迁移：加列 → 回填 → 核对，**顺序不许倒**。

这条迁移是本步最危险的一处（ADR-0025 决策七）：先启用版本过滤、后回填的话，
历史切片的版本列是空值，``(kb AND version)`` 一条都匹配不上——**不报错，只是搜不到**，
看起来像「这个库没内容」。所以这里守的不是「列加上了」，而是四件事：

1. **链与 head**：本票接在当前**唯一** head 上（#146 也可能加迁移，所以不写死某个 revision，
   而是「我接在其余迁移的唯一 head 上」）；
2. **顺序**：``add_column`` 在前、``UPDATE`` 回填在中、核对语句在最后——把三段在
   ``executed`` 里的位置拿来比，而不是断言「语句里有 UPDATE 这回事」；
3. **回填写在迁移里**（不是脚本）：生效版本列被写成 ``v1``，并为每个库建一行
   ``index_build_versions``（状态 ``active``、四项计数里能从本库取到的两项是快照）；
4. **核对会真的拦人**：回填没做完时 ``upgrade()`` **抛错中止**（DDL 与 DML 在同一个
   事务里，于是不会留下「列加了、过滤却已启用」的半成品）；``downgrade()`` 之后库的形状
   与本次之前**逐项相等**。

不需要真数据库：``op`` 是一个有状态的记录器，``get_bind()`` 返回它自己，
于是「库里现在有哪些表 / 列 / 索引」和「核对语句读到了什么」都是可以逐项比对的事实。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票的迁移（文件名 = 12 位 revision + 这个 slug）。
SLUG = "add_index_build_versions"

#: 本票之前库里已有的形状：只建到这一步之前就存在的那几张表里、本票会碰到的那两张。
#: 逐列抄自 ``app/models``（``knowledge_bases`` 与 ``retrieval_traces``），不含本票新增列。
KB_COLUMNS = {
    "id": False,
    "tenant_id": False,
    "name": False,
    "description": True,
    "split_strategy": False,
    "allowed_formats": False,
    "settings": False,
    "created_at": False,
    "updated_at": False,
}
TRACE_COLUMNS = {
    "id": False,
    "tenant_id": False,
    "trace_id": False,
    "kb_ids": False,
    "chunk_ids": False,
    "top_k": False,
    "latency_ms": False,
    "degraded_reason": True,
    "empty_reason": True,
    "created_at": False,
}

#: 新表只允许这十一列（与 ``app/models/index_version.py`` 逐字一致）。
EXPECTED_VERSION_COLUMNS = frozenset(
    {
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
    }
)

#: 五种状态（ADR-0025 决策一）。约束在库里，不在代码注释里。
EXPECTED_STATUSES = ("building", "ready", "active", "archived", "failed")

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _FakeResult:
    """核对语句的返回值：只回答「还有几个库没回填」。"""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _RecordingOp:
    """有状态的假 ``op``：维护「库里现在有哪些表 / 列 / 索引」，并记录每条 DML。"""

    def __init__(self, *, unbackfilled_kbs: int = 0, as_sql: bool = False) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {
            "knowledge_bases": {
                name: {"nullable": nullable, "type": "prior", "default": None}
                for name, nullable in KB_COLUMNS.items()
            },
            "retrieval_traces": {
                name: {"nullable": nullable, "type": "prior", "default": None}
                for name, nullable in TRACE_COLUMNS.items()
            },
        }
        self.indexes: dict[str, dict[str, Any]] = {}
        self.constraints: dict[str, list[Any]] = {}
        self.executed: list[str] = []
        #: 核对语句会读到几个「还没回填的库」；默认 0（回填做完了）。
        self.unbackfilled_kbs = unbackfilled_kbs
        #: True 即 ``alembic upgrade --sql`` 那种离线渲染：没有库可查。
        self.as_sql = as_sql

    # ── 迁移用到的几个 op ──

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> "_RecordingOp":
        """核对语句走 ``op.get_bind().execute(...)``（``op.execute`` 不返回行）。"""

        return self

    def get_context(self) -> Any:
        """迁移据此分辨「连库执行」与「离线渲染」两种模式。"""

        return SimpleNamespace(as_sql=self.as_sql)

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _FakeResult:
        self.executed.append(str(statement))
        return _FakeResult(self.unbackfilled_kbs)

    def create_table(self, table_name: str, *args: Any, **_kwargs: Any) -> None:
        assert table_name not in self.tables, f"表已存在：{table_name}"
        self.executed.append(f"ddl:create_table {table_name}")
        # 真的挂到一张（一次性）表上：约束的列名只有被 attach 之后才读得出来，
        # 否则只能去摸私有属性。
        table = sa.Table(table_name, sa.MetaData(), *args)
        self.tables[table_name] = {column.name: _spec(column) for column in table.columns}
        self.constraints[table_name] = [
            item for item in table.constraints if not isinstance(item, sa.PrimaryKeyConstraint)
        ]

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        self.tables.pop(table_name)
        self.constraints.pop(table_name, None)
        for index_name in [
            name for name, spec in self.indexes.items() if spec["table"] == table_name
        ]:
            self.indexes.pop(index_name)

    def add_column(self, table_name: str, column: Any) -> None:
        assert table_name in self.tables, table_name
        self.executed.append(f"ddl:add_column {table_name}.{column.name}")
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
        existing_type: Any = None,
        nullable: Any = None,
        server_default: Any = None,
        **_kwargs: Any,
    ) -> None:
        spec = self.tables[table_name][column_name]
        if nullable is not None:
            spec["nullable"] = bool(nullable)
        if server_default is not None:
            spec["default"] = str(server_default)

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: Any,
        *,
        unique: bool = False,
        postgresql_where: Any = None,
        **_kwargs: Any,
    ) -> None:
        assert table_name in self.tables, table_name
        self.indexes[index_name] = {
            "table": table_name,
            "columns": tuple(columns),
            "unique": bool(unique),
            "where": None if postgresql_where is None else str(postgresql_where),
        }

    def drop_index(self, index_name: str, table_name: str | None = None, **_kwargs: Any) -> None:
        spec = self.indexes.pop(index_name, None)
        assert spec is not None, f"要删的索引不存在：{index_name}"
        if table_name is not None:
            assert spec["table"] == table_name, f"{index_name} 属于 {spec['table']}"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}")

    # ── 断言用的便利读法 ──

    def columns_of(self, table_name: str) -> frozenset[str]:
        return frozenset(self.tables[table_name])


def _spec(column: Any) -> dict[str, Any]:
    default = getattr(column, "server_default", None)
    return {
        "type": str(column.type),
        "nullable": bool(column.nullable),
        "default": None if default is None else str(default),
    }


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t141_migration_{path.stem}", path)
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


def _migrate(
    direction: str = "upgrade", *, unbackfilled_kbs: int = 0, as_sql: bool = False
) -> _RecordingOp:
    """跑一遍本票的迁移；``downgrade`` 总是**先升后降**（真实用法就是如此）。"""

    module = _load_migration(_migration_path(SLUG))
    recorder = _RecordingOp(unbackfilled_kbs=unbackfilled_kbs, as_sql=as_sql)
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


def test_it_chains_onto_the_single_current_head() -> None:
    """接在**其余迁移的唯一 head** 上，且自己不另起一头。

    不写死 ``down_revision == a91f3c7e2058``：#146 也在加迁移，真实接点是「当时的链尾」；
    这里要守的性质是「接得上、只有一条链」，不是「永远接在某一条特定 revision 后」。
    """

    mine = _revision_of(_migration_path(SLUG))
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(path): path for path in paths}
    down_revisions = {_down_revision_of(path) for path in paths} - {None}
    heads = set(revisions) - down_revisions

    assert len(heads) == 1, f"全链必须只有一条链，实际 head={sorted(heads)}"

    # 本票在**这条链上**：从唯一 head 沿 down_revision 一路走能走到它。
    # 「本票就是链尾」不是它的固有性质——后续票接一条迁移在它后面是正常演化
    # （#152 的关系边表就接在它之后），链尾前移不该让这条用例变红。
    by_revision = {_revision_of(path): _down_revision_of(path) for path in paths}
    chain: set[str] = set()
    cursor: str | None = next(iter(heads))
    while cursor is not None:
        chain.add(cursor)
        cursor = by_revision[cursor]

    assert mine in chain, "本票必须仍在这条唯一的链上"


# ──────────────────────────────────────────────────────────── upgrade：先加列、再回填


def test_upgrade_creates_the_version_table_with_exactly_the_enumerated_columns() -> None:
    recorder = _migrate()

    assert recorder.columns_of("index_build_versions") == set(EXPECTED_VERSION_COLUMNS)


def test_upgrade_constrains_status_and_pins_version_names_per_kb() -> None:
    """两个约束进库：状态是那五个值；同名版本在一个库里只能有一行。"""

    recorder = _migrate()
    constraints = recorder.constraints["index_build_versions"]

    checks = [item for item in constraints if isinstance(item, sa.CheckConstraint)]
    uniques = [item for item in constraints if isinstance(item, sa.UniqueConstraint)]

    assert checks, "状态必须有 CHECK，不是靠代码自觉"
    rendered = " ".join(str(item.sqltext) for item in checks)
    for status in EXPECTED_STATUSES:
        assert status in rendered, f"CHECK 里少了状态 {status}"

    assert uniques, "同一库内版本名唯一"
    assert any(tuple(item.columns.keys()) == ("tenant_id", "kb_id", "version") for item in uniques)


def test_upgrade_allows_at_most_one_active_and_one_building_row_per_kb() -> None:
    """两条部分唯一索引：同一库同一时刻最多一个 active、一个 building（ADR-0025 决策一）。"""

    recorder = _migrate()
    partial = {
        name: spec
        for name, spec in recorder.indexes.items()
        if spec["table"] == "index_build_versions"
    }
    by_status = {str(spec["where"]): name for name, spec in partial.items() if spec["where"]}

    assert set(by_status) == {"status = 'active'", "status = 'building'"}, (
        f"部分唯一索引的谓词必须正好是这两种，实际 {sorted(by_status)}"
    )
    for name in by_status.values():
        assert partial[name]["unique"] is True
        assert partial[name]["columns"] == ("tenant_id", "kb_id")


def test_upgrade_adds_the_kb_effective_version_column_as_nullable() -> None:
    """库上的生效版本**可空**：回填没做完时它必须是 NULL，守卫才有东西可看。"""

    recorder = _migrate()
    spec = recorder.tables["knowledge_bases"]["active_index_version"]

    assert spec["nullable"] is True, "写成 NOT NULL 就等于把「未回填」这个状态抹掉了"
    assert spec["type"].upper().startswith("VARCHAR"), "版本名是短字符串（与 chunks 同宽）"


def test_upgrade_adds_the_trace_version_mapping_column() -> None:
    recorder = _migrate()
    spec = recorder.tables["retrieval_traces"]["index_versions"]

    assert spec["type"].upper() == "JSONB"
    assert spec["nullable"] is True, "迁移之前写下的行没有版本可记，回填不能凭空造"


def test_upgrade_adds_columns_first_then_backfills_then_verifies() -> None:
    """顺序是本票的**核心**：加列 → 回填 → 核对。倒过来就是历史数据静默变空。"""

    recorder = _migrate()
    statements = recorder.executed

    assert len(statements) >= 3, "至少要有一条回填语句与一条核对语句"

    added = [index for index, sql in enumerate(statements) if sql.startswith("ddl:add_column")]
    backfill = [
        index for index, sql in enumerate(statements) if "update knowledge_bases" in sql.lower()
    ]
    version_rows = [
        index
        for index, sql in enumerate(statements)
        if "insert into index_build_versions" in sql.lower()
    ]
    verification = [
        index
        for index, sql in enumerate(statements)
        if "select count(*)" in sql.lower() and "active_index_version is null" in sql.lower()
    ]

    assert len(added) == 2, "两列（库上的生效版本、归属索引上的映射）都要先加上"
    assert backfill and version_rows and verification, (
        "回填（库行 + 版本行）与核对语句都必须真的写在迁移里"
    )
    assert max(added) < min(backfill), "加列必须早于任何回填"
    assert max(backfill) < min(verification), "回填必须早于核对"
    assert max(version_rows) < min(verification), "版本行必须早于核对"


def test_upgrade_backfills_the_effective_version_to_v1() -> None:
    recorder = _migrate()
    statements = " ".join(recorder.executed).lower()

    assert "set active_index_version = 'v1'" in statements, "历史库一律回填 v1（当前切片就是 v1）"


def test_upgrade_snapshots_the_two_counts_it_can_see_from_this_database() -> None:
    """为每个库建一行 ``active`` 的 v1 版本，计数快照取自本次迁移能读到的两张表。

    向量数与关键词文档数是**别的存储**里的东西，迁移读不到——那两项属于 ``ready`` 的
    前置校验（#142 的激活流程），这里只落 ``document_count`` / ``chunk_count``。
    """

    recorder = _migrate()
    inserts = [
        sql
        for sql in recorder.executed
        if sql.strip().lower().startswith("insert into index_build_versions")
    ]

    assert len(inserts) == 1, "一条 INSERT ... SELECT 覆盖所有库，不按库循环"
    sql = inserts[0].lower()
    assert "from knowledge_bases" in sql
    assert "document_count" in sql and "chunk_count" in sql
    assert "'active'" in sql, "回填出来的 v1 就是当前生效版本"


def test_upgrade_refuses_to_finish_when_a_kb_was_not_backfilled() -> None:
    """守卫会真的拦人：核对读到未回填的库就中止迁移，而不是留下半成品。"""

    module = _load_migration(_migration_path(SLUG))
    recorder = _RecordingOp(unbackfilled_kbs=2)
    module.op = recorder

    with pytest.raises(RuntimeError) as error:
        module.upgrade()

    assert "knowledge_bases" in str(error.value) or "回填" in str(error.value)


def test_offline_sql_rendering_carries_an_aborting_check_of_its_own() -> None:
    """``alembic upgrade --sql`` 没有库可查：同一段判断要以 DO 块形式留在脚本里。

    否则离线渲染要么崩在 ``op.get_bind()`` 上，要么渲染出一份「核对静默消失」的脚本
    ——后者更糟：照着它执行的人会以为回填被核对过。
    """

    recorder = _migrate(as_sql=True, unbackfilled_kbs=2)

    blocks = [sql for sql in recorder.executed if "DO $$" in sql]
    assert len(blocks) == 1, "离线渲染时核对必须落成一条 DO 块"
    sql = blocks[0].lower()
    assert "raise exception" in sql, "渲染出的脚本被真正执行时要能自己中止"
    assert "active_index_version is null" in sql, "判断条件必须与在线那一刻是同一条"
    assert "index_build_versions" in sql, "也要查「版本行是否存在」"
    assert recorder.executed[-1] is blocks[0], "核对在最后，回填在它之前"


def test_online_mode_does_not_emit_the_aborting_block() -> None:
    """连库执行时用 Python 判断（能就地报出「还有几个库」），不靠抛 SQL 异常。"""

    recorder = _migrate()

    assert not [sql for sql in recorder.executed if "DO $$" in sql]


def test_upgrade_leaves_the_effective_version_nullable_after_backfill() -> None:
    """回填**不**把列收成 NOT NULL：新的空库还没有生效版本，那是合法状态。"""

    recorder = _migrate()

    assert recorder.tables["knowledge_bases"]["active_index_version"]["nullable"] is True


# ──────────────────────────────────────────────────────────────── downgrade：退得回去


def test_downgrade_restores_the_previous_shape_exactly() -> None:
    """回滚之后两张表逐列等于迁移之前，新表与新索引一个都不留。"""

    before = _RecordingOp()
    recorder = _migrate("downgrade")

    assert "index_build_versions" not in recorder.tables
    for table_name in ("knowledge_bases", "retrieval_traces"):
        assert recorder.columns_of(table_name) == set(before.columns_of(table_name)), (
            f"{table_name} 回滚后必须与原形状逐列相等"
        )
        assert not [
            name for name, spec in recorder.indexes.items() if spec["table"] == table_name
        ], f"{table_name} 上不该留下本票的索引"


def test_downgrade_drops_the_added_columns_even_though_they_carried_data() -> None:
    """降级就是放弃「哪次构建」这个能力：列必须一起走，不能留一列没人维护。"""

    recorder = _migrate("downgrade")

    assert "active_index_version" not in recorder.columns_of("knowledge_bases")
    assert "index_versions" not in recorder.columns_of("retrieval_traces")


# ─────────────────────────────────────────────────────── 模型与迁移必须是同一份契约


def test_the_model_declares_the_same_table_as_the_migration() -> None:
    """ORM 与迁移各写一遍列名，两者一旦漂移，检索读到的就是一张另一张表。"""

    from app.models.index_version import INDEX_BUILD_STATUSES, IndexBuildVersion

    assert IndexBuildVersion.__tablename__ == "index_build_versions"
    assert {column.key for column in IndexBuildVersion.__table__.columns} == set(
        EXPECTED_VERSION_COLUMNS
    )
    assert set(EXPECTED_STATUSES) == set(INDEX_BUILD_STATUSES)

    # 两条部分唯一索引也在模型上：迁移只是把模型里的约束照抄进库。
    partial = {
        str(index.dialect_options["postgresql"]["where"])
        for index in IndexBuildVersion.__table__.indexes
        if index.dialect_options["postgresql"]["where"] is not None
    }
    assert partial == {"status = 'active'", "status = 'building'"}


def test_the_two_existing_tables_declare_the_columns_this_migration_adds() -> None:
    from app.models.knowledge_base import KnowledgeBase
    from app.models.retrieval_trace import RetrievalTrace

    kb_column = KnowledgeBase.__table__.columns["active_index_version"]
    trace_column = RetrievalTrace.__table__.columns["index_versions"]

    assert kb_column.nullable is True, "「还没有生效构建」必须是可表达的状态"
    assert trace_column.nullable is True, "迁移之前的行没有版本可记"
    assert str(trace_column.type) == "JSONB", "映射必须是一个 JSON 对象，不是两个数组"
