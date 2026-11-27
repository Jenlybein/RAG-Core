"""现场九第一步的迁移（加「索引代」三列 + 回填 + 停点对账）。

三条纪律，与既有的迁移用例同一套：

1. **接在链尾、全链只有一个 head**；
2. **可升可降且对称**：升的时候建了什么，降的时候就要拆掉什么；
3. **停点不通过就必须中止**——这条是本轮特有的：回填错了要停在第一步，不能带着错的
   回填继续往下删列（那样连反推的原料都没了）。

除此之外还钉住两件**纯函数**的口径（指纹与分代），因为迁移与运行时必须共用同一份实现：
写两遍就会分叉，而分叉的症状是「设置没改，界面却说全库待更新」。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.services import index_generation
from app.services.index_generation import (
    FIRST_GENERATION,
    SUPERSEDED_GENERATION,
    generation_of,
    is_stale,
    settings_fingerprint,
)

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本步的迁移（slug 与文件名的后半段一致）。
NEW_MIGRATION_SLUG = "add_index_generation"

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
    """``bind.execute(...).fetchall() / .fetchone()`` 的最小替身。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _Bind:
    """按脚本返回结果：先给参数行（指纹回填），再给停点那一行。"""

    def __init__(self, *, params_rows: list[Any] | None = None, stop_row: Any = (0, 0)) -> None:
        self.queued = [_Result(params_rows or []), _Result([stop_row])]
        self.statements: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        self.statements.append(str(statement))
        if not self.queued:
            return _Result([])
        return self.queued.pop(0)


class _Recorder:
    """把 ``alembic.op`` 换成记录器：只关心「建了什么/拆了什么/跑了什么 SQL」。"""

    def __init__(self, *, bind: _Bind | None = None) -> None:
        self._bind = bind or _Bind()
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


def test_the_whole_chain_has_exactly_one_head_and_it_is_this_migration() -> None:
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(path) for path in paths}
    downs = {
        match.group("down")
        for path in paths
        if (match := _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))) is not None
    }
    heads = revisions - downs
    assert len(heads) == 1, f"迁移链必须只有一个 head，实际 {sorted(heads)}"
    assert heads == {_revision_of(_migration_path(NEW_MIGRATION_SLUG))}


# ───────────────────────────────────────────────────── 结构：只加三列 + 一个索引


def test_it_adds_exactly_the_three_columns(migration: tuple[ModuleType, _Recorder]) -> None:
    module, recorder = migration
    module.upgrade()

    assert recorder.columns == [
        ("documents", "current_index_generation", False),
        ("documents", "settings_hash", True),
        ("chunks", "generation", False),
    ], "三列的名字与可空性都是契约（指纹允许空 = 来路不明）"


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
    assert "THEN 1" in sql and "ELSE 0" in sql


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
    module.upgrade()  # 记录器给的停点行是 (0, 0)，不该抛

    assert recorder.columns, "走到这里说明没被中止"


@pytest.mark.parametrize(
    ("stop_row", "hint"),
    [
        ((1, 0), "逐篇对不上"),
        ((0, 2), "切过去会查不到"),
        ((3, 3), "两种问题同时出现"),
    ],
)
def test_the_stop_point_aborts_the_migration_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, stop_row: tuple[int, int], hint: str
) -> None:
    """回填错了必须停在第一步：旧列与旧表一个都还在，回滚只回滚一步。

    这条用例守住的是「不要带着错的回填继续往下删列」——删完才发现，连反推的原料都没了。
    """

    module = _load_migration(NEW_MIGRATION_SLUG)
    recorder = _Recorder(bind=_Bind(stop_row=stop_row))
    monkeypatch.setattr(module, "op", recorder)

    with pytest.raises(RuntimeError) as caught:
        module.upgrade()

    message = str(caught.value)
    assert "停点检查没通过" in message, hint
    assert "旧列与旧表一个都没删" in message, "要让人知道现在的状态是安全的"
    assert "重建一次" in message, "要给可操作的下一步"


# ───────────────────────────────────────────────────── 纯函数：指纹


def test_the_fingerprint_is_key_order_independent() -> None:
    """同样的设置、不同的构造顺序，指纹必须一样。

    不然症状是「设置没改，界面却说全库待更新」，而且只在某些路径下出现——最难查的那种。
    """

    left = settings_fingerprint({"chunk_size": 500, "split_strategy": "structure"})
    right = settings_fingerprint({"split_strategy": "structure", "chunk_size": 500})

    assert left == right
    assert left is not None and len(left) == index_generation.FINGERPRINT_HEX_LENGTH


def test_a_different_setting_gets_a_different_fingerprint() -> None:
    assert settings_fingerprint({"chunk_size": 500}) != settings_fingerprint({"chunk_size": 501})


def test_an_unknown_fingerprint_stays_none_instead_of_becoming_an_empty_string() -> None:
    """「没记过」与「和当前设置不一样」是两件事，日志里必须分得开。"""

    assert settings_fingerprint(None) is None


# ───────────────────────────────────────────────────── 纯函数：分代与是否落后


def test_the_active_version_is_the_first_generation_and_the_rest_are_superseded() -> None:
    assert generation_of("v7", "v7") == FIRST_GENERATION
    assert generation_of("v3", "v7") == SUPERSEDED_GENERATION


@pytest.mark.parametrize(("index_version", "active"), [(None, "v7"), ("v7", None), (None, None)])
def test_an_unknown_version_is_never_treated_as_the_current_product(
    index_version: str | None, active: str | None
) -> None:
    """来路不明的切片不能认成线上内容——认错比认不出更坏。"""

    assert generation_of(index_version, active) == SUPERSEDED_GENERATION


@pytest.mark.parametrize(
    ("stored", "current", "stale"),
    [
        ("abc", "abc", False),
        ("abc", "def", True),
        (None, "abc", True),
        ("abc", None, True),
        (None, None, True),
    ],
)
def test_a_document_is_stale_unless_both_fingerprints_are_known_and_equal(
    stored: str | None, current: str | None, stale: bool
) -> None:
    assert is_stale(stored, current) is stale
