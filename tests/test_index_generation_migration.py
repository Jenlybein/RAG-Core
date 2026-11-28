"""现场九第一步的迁移（加「索引代」三列 + 回填 + 停点对账）。

三条纪律，与既有的迁移用例同一套：

1. **接在链尾、全链只有一个 head**；
2. **可升可降且对称**：升的时候建了什么，降的时候就要拆掉什么；
3. **停点不通过就必须中止**——这条是本轮特有的：回填错了要停在第一步，不能带着错的
   回填继续往下删列（那样连反推的原料都没了）。

两条**独立的**停点检查各自有用例，因为它们抓的是两种不同的坏法，而且第一条的
``WHERE backfilled <> expected`` 会把第二条（零片）那一类**过滤掉**——写成一条 SQL
时那个守卫会永远返回 0，等于写成了一句安慰。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.models.chunk import DEFAULT_GENERATION
from app.services import index_generation
from app.services.index_generation import (
    CANONICAL_PARAM_KEYS,
    SUPERSEDED_GENERATION,
    settings_fingerprint,
)

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本步的迁移（slug 与文件名的后半段一致）。
NEW_MIGRATION_SLUG = "add_index_generation"

#: 链尾：接在本条迁移后面的那几条（旧代回收的索引、归属索引改记篇级代、
#: 库级索引版本整层退场）。本条不再是 head 之后，「全链只有一个 head」仍然要守，
#: 只是那个 head 换成了最后那一条。
CHAIN_TAIL_SLUG = "drop_index_version_layer"

#: 上一条 head（现场五的过渡密钥四列）。
PREVIOUS_HEAD = "e4b1d7a9c2f5"

_REVISION_RE = re.compile(
    r"""^revision(?::\s*[^=]+)?\s*=\s*["'](?P<revision>[0-9a-f]{12})["']""", re.M
)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*["'](?P<down>[0-9a-f]{12})["']""", re.M
)


# ───────────────────────────────────────────────────── 载入迁移模块


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"找不到唯一的迁移文件：{slug} → {matches}"
    return matches[0]


def _load_migration(slug: str) -> ModuleType:
    path = _migration_path(slug)
    spec = importlib.util.spec_from_file_location(f"migration_{slug}", path)
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


# ───────────────────────────────────────────────────── 记录器 op


class _Result:
    """``bind.execute(...).fetchall() / .scalar()`` 的最小替身。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def scalar(self) -> Any:
        if not self._rows:
            return None
        first = self._rows[0]
        return first[0] if isinstance(first, tuple) else first


class _Bind:
    """按脚本返回结果：先给参数行（指纹回填），再依次给两条停点检查的读数。"""

    def __init__(
        self,
        *,
        params_rows: list[Any] | None = None,
        mismatched: int = 0,
        empty_products: int = 0,
    ) -> None:
        self.queued = [
            _Result(params_rows or []),
            _Result([(mismatched,)]),
            _Result([(empty_products,)]),
        ]
        self.statements: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        self.statements.append(str(statement))
        if not self.queued:
            return _Result([])
        return self.queued.pop(0)


class _Context:
    def __init__(self, *, as_sql: bool) -> None:
        self.as_sql = as_sql


class _Recorder:
    """把 ``alembic.op`` 换成记录器：只关心「建了什么/拆了什么/跑了什么 SQL」。"""

    def __init__(self, *, bind: _Bind | None = None, as_sql: bool = False) -> None:
        self._bind = bind or _Bind()
        self._context = _Context(as_sql=as_sql)
        self.columns: list[tuple[str, str, bool]] = []
        self.indexes: list[tuple[str, str, tuple[str, ...]]] = []
        self.executed: list[str] = []
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    # 升
    def add_column(self, table: str, column: Any) -> None:
        self.columns.append((table, column.name, bool(column.nullable)))

    def create_index(self, name: str, table: str, columns: list[str], **_: Any) -> None:
        self.indexes.append((name, table, tuple(columns)))

    def execute(self, statement: Any) -> None:
        self.executed.append(str(statement))

    def get_bind(self) -> _Bind:
        return self._bind

    def get_context(self) -> _Context:
        return self._context

    # 降
    def drop_index(self, name: str, table_name: str | None = None, **_: Any) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_column(self, table: str, name: str) -> None:
        self.dropped_columns.append((table, name))


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, _Recorder]:
    module = _load_migration(NEW_MIGRATION_SLUG)
    recorder = _Recorder()
    monkeypatch.setattr(module, "op", recorder)
    return module, recorder


# ───────────────────────────────────────────────────── 链与命名


def test_the_migration_is_named_after_its_twelve_hex_revision() -> None:
    path = _migration_path(NEW_MIGRATION_SLUG)
    assert path.stem.startswith(f"{_revision_of(path)}_"), "文件名必须以 revision 开头"


def test_the_migration_chains_onto_the_previous_head() -> None:
    path = _migration_path(NEW_MIGRATION_SLUG)
    match = _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None
    assert match.group("down") == PREVIOUS_HEAD


def test_the_whole_chain_has_exactly_one_head() -> None:
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(path) for path in paths}
    downs = {
        match.group("down")
        for path in paths
        if (match := _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))) is not None
    }
    heads = revisions - downs
    assert len(heads) == 1, f"迁移链必须只有一个 head，实际 {sorted(heads)}"
    assert heads == {_revision_of(_migration_path(CHAIN_TAIL_SLUG))}, (
        "本条后面还有三条（旧代回收的索引、归属索引改记篇级代、删库级版本层），head 是最后那一条"
    )


# ───────────────────────────────────────────────────── 结构：只加三列 + 一个索引


def test_it_adds_exactly_the_three_columns(migration: tuple[ModuleType, _Recorder]) -> None:
    module, recorder = migration
    module.upgrade()

    assert recorder.columns == [
        # 文档代数可空：NULL = 「还没有任何一代产物」，与「第 1 代但恰好为空」分开。
        ("documents", "current_index_generation", True),
        ("documents", "settings_hash", True),
        ("chunks", "generation", False),
    ]


def test_it_builds_the_generation_index_for_retrieval_and_retention(
    migration: tuple[ModuleType, _Recorder],
) -> None:
    module, recorder = migration
    module.upgrade()

    assert recorder.indexes == [
        ("ix_chunks_tenant_id_kb_id_generation", "chunks", ("tenant_id", "kb_id", "generation"))
    ]


def test_it_never_drops_anything_on_the_way_up(
    migration: tuple[ModuleType, _Recorder],
) -> None:
    """本步只做加法：删旧列与旧表是下一条迁移。停点的全部意义就在这一条上。"""

    module, recorder = migration
    module.upgrade()

    assert recorder.dropped_columns == []
    assert recorder.dropped_indexes == []


def test_backfill_marks_active_version_chunks_as_the_first_generation(
    migration: tuple[ModuleType, _Recorder],
) -> None:
    module, recorder = migration
    module.upgrade()

    backfill = [sql for sql in recorder.executed if "UPDATE chunks" in sql]
    assert len(backfill) == 1, "切片分代只该有一条 SQL"
    sql = " ".join(backfill[0].split())
    assert "kb.active_index_version" in sql, "判据必须是比较库级生效版本名"
    assert f"THEN {DEFAULT_GENERATION}" in sql
    assert f"ELSE {SUPERSEDED_GENERATION}" in sql


def test_a_document_without_any_chunk_gets_no_generation(
    migration: tuple[ModuleType, _Recorder],
) -> None:
    """「还没有产物」是 NULL，不是第 1 代。

    两件事在检索侧处理不同：NULL 跳过并计数，而「第 1 代恰好零片」是一次正常的空结果。
    用一个值表示两件事，第三根轴还没上线就已经说不清了。
    """

    module, recorder = migration
    module.upgrade()

    documents_sql = [sql for sql in recorder.executed if "UPDATE documents" in sql]
    assert documents_sql, "文档代数必须回填"
    sql = " ".join(documents_sql[0].split())
    assert "THEN 1" in sql and "ELSE NULL" in sql


def test_downgrade_is_symmetric(migration: tuple[ModuleType, _Recorder]) -> None:
    module, recorder = migration
    module.downgrade()

    assert recorder.dropped_indexes == [("ix_chunks_tenant_id_kb_id_generation", "chunks")]
    assert recorder.dropped_columns == [
        ("chunks", "generation"),
        ("documents", "settings_hash"),
        ("documents", "current_index_generation"),
    ]


# ───────────────────────────────────────────────────── 停点：不通过就中止


def test_the_stop_point_passes_when_every_document_matches(
    migration: tuple[ModuleType, _Recorder],
) -> None:
    module, recorder = migration
    module.upgrade()  # 记录器给的两条读数都是 0，不该抛

    assert recorder.columns, "走到这里说明没被中止"


@pytest.mark.parametrize(
    ("mismatched", "empty_products"),
    [
        (1, 0),  # 回填漏了（典型：切片所属的库行已不在，保住列缺省值）
        (0, 2),  # 切过去之后会查不到
        (3, 3),  # 两种问题同时出现
    ],
)
def test_the_stop_point_aborts_the_migration_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, mismatched: int, empty_products: int
) -> None:
    """回填错了必须停在第一步：旧列与旧表一个都还在，回滚只回滚一步。"""

    module = _load_migration(NEW_MIGRATION_SLUG)
    recorder = _Recorder(bind=_Bind(mismatched=mismatched, empty_products=empty_products))
    monkeypatch.setattr(module, "op", recorder)

    with pytest.raises(RuntimeError) as caught:
        module.upgrade()

    message = str(caught.value)
    assert "停点检查没通过" in message
    assert "旧列与旧表一个都没删" in message, "要让人知道现在的状态是安全的"
    assert f"逐篇对不上的文档数={mismatched}" in message
    assert f"切过去会查不到」的篇数={empty_products}" in message
    assert "重建一次" in message, "要给可操作的下一步"


def test_the_two_stop_point_checks_are_separate_queries(
    migration: tuple[ModuleType, _Recorder],
) -> None:
    """零片那几篇必须由**另一条**查询数出来。

    并成一条时，``WHERE backfilled <> expected`` 会把「零片且期望零片」整行过滤掉，
    于是第二个读数永远返回 0——守卫写成了一句安慰。
    """

    module, recorder = migration
    module.upgrade()

    stop_queries = [sql for sql in recorder._bind.statements if "documents" in sql]
    assert len(stop_queries) >= 2, "两条检查必须是两条查询"
    consistency, empty_products = stop_queries[-2], stop_queries[-1]
    assert "backfilled <> expected" in " ".join(consistency.split())
    assert "backfilled <> expected" not in " ".join(empty_products.split()), (
        "零片读数不能被不等式过滤"
    )
    assert "NOT EXISTS" in " ".join(empty_products.split())


def test_an_offline_render_is_refused_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--sql`` 离线渲染第一行就拒：不留半份 DDL 脚本。

    离线时 ``op.get_bind()`` 返回的是 alembic 的 ``MockConnection``（**永远不为 None**），
    所以判据只能读 ``op.get_context().as_sql``；否则会先写下半份 DDL，再在一个
    看不懂的 ``AttributeError`` 上崩。
    """

    module = _load_migration(NEW_MIGRATION_SLUG)
    recorder = _Recorder(as_sql=True)
    monkeypatch.setattr(module, "op", recorder)

    with pytest.raises(RuntimeError) as caught:
        module.upgrade()

    assert "需要真库" in str(caught.value)
    assert recorder.columns == [], "拒绝必须发生在任何 DDL 之前"


# ───────────────────────────────────────────────────── 纯函数：指纹


def _params(**overrides: Any) -> dict[str, Any]:
    """一份完整的规范参数（三个键齐全）——缺键时指纹按「未知」返回 ``None``，那是另一条用例。"""

    base: dict[str, Any] = {
        "split_strategy": "structure",
        "chunk_size": 500,
        "section_part_max_chars": 2000,
    }
    return {**base, **overrides}


def test_the_fingerprint_is_key_order_independent() -> None:
    """同样的设置、不同的构造顺序，指纹必须一样。

    不然症状是「设置没改，界面却说全库待更新」，而且只在某些路径下出现——最难查的那种。
    """

    left = settings_fingerprint(
        {"chunk_size": 500, "split_strategy": "structure", "section_part_max_chars": 2000}
    )
    right = settings_fingerprint(
        {"split_strategy": "structure", "chunk_size": 500, "section_part_max_chars": 2000}
    )

    assert left == right
    assert left is not None and len(left) == index_generation.FINGERPRINT_HEX_LENGTH


def test_only_the_canonical_keys_participate() -> None:
    """快照里多出来的键（老版本写过、后来删掉）不参与指纹。

    参与了就有一个很难看的后果：设置一个字没改，界面却说全库待更新——而那种提示一旦
    误报过一次，人就学会永久忽略它。
    """

    canonical = _params()
    polluted = {**canonical, "legacy_key_from_some_old_version": 7}

    assert settings_fingerprint(canonical) == settings_fingerprint(polluted)


def test_a_snapshot_missing_a_canonical_key_is_unknown_rather_than_guessed() -> None:
    """缺键 = 说不清它用的是哪套设置，按「未知」处理，而不是拿剩下的键凑一个指纹。"""

    partial = _params()
    partial.pop("chunk_size")
    assert settings_fingerprint(partial) is None


def test_a_different_setting_gets_a_different_fingerprint() -> None:
    assert settings_fingerprint(_params(chunk_size=500)) != settings_fingerprint(
        _params(chunk_size=501)
    )


def test_an_unknown_fingerprint_stays_none_instead_of_becoming_an_empty_string() -> None:
    """「没记过」与「和当前设置不一样」是两件事，日志里必须分得开。"""

    assert settings_fingerprint(None) is None


def test_the_first_generation_has_exactly_one_literal() -> None:
    """「第 1 代」只在模型层写一次：迁移、运行时、测试都指向它。

    分头写 1 迟早有一处漂移，而漂移的症状是**新旧产物被算成不同代**——界面从此永远显示
    「待更新」，而每一次点更新都真的在白烧 embedding。
    """

    assert index_generation.FIRST_GENERATION is DEFAULT_GENERATION


def test_the_canonical_keys_come_from_one_place() -> None:
    """规范键集必须就是 ``build_params_of`` 那三项，不另抄一份。"""

    assert CANONICAL_PARAM_KEYS == ("split_strategy", "chunk_size", "section_part_max_chars")
