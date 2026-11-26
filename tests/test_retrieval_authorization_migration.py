"""第 13 步 · T1（expand）：新授权/计量列的迁移。

纪律与既有加列迁移一致（先例 ``test_research_quota_migration.py``）：

1. 接在链尾（``down_revision`` 指向 T1 之前的唯一 head），整条链仍只有一个 head；
2. **纯加列**：不动旧列、不删任何东西——这是 expand 段，旧形式留给 T8 删；
3. 每张表的新列都 ``NOT NULL`` + ``server_default``，存量行升级那一刻拿到合法缺省，
   于是「升级完的库」与「改造前的库」跑出同一个结果（新列此刻还不参与判定）；
4. 可升可降：``downgrade`` 只删本票加的列，别的一列不碰。

列名与缺省值不从这个仓库的模块里反推——那份清单由被测模块自己给，这里用 spec 里
逐项拍过的字面量当独立真值。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

SLUG = "add_retrieval_authorization"
PREVIOUS_HEAD = "3f7b2c9d5e14"

#: 真正**新增**的三个授权位（布尔，缺省 false）。``research_allowed`` /
#: ``rerank_allowed`` 两张表早已有列，第 13 步复用它们，不许重复 add_column。
CAPABILITY_COLUMNS = (
    "retrieve_allowed",
    "rewrite_allowed",
    "decompose_allowed",
)

#: 三本账 × 两格 = 6 列。其中 ``retrieve_qps`` / ``retrieve_daily`` **本来就存在**
#: （它们就是「retrieve 那本账」），expand 段只补另外四列；T8 删的是旧的
#: ``research_qps`` / ``research_daily``，不是这两列。
EXISTING_METER_COLUMNS = ("retrieve_qps", "retrieve_daily")
NEW_METER_COLUMNS = (
    "rerank_qps",
    "rerank_daily",
    "llm_qps",
    "llm_daily",
)

#: 两张表各一份同名列；max_top_k 只在 plans 上（它是套餐级上限）。
PLAN_COLUMNS = (*CAPABILITY_COLUMNS, *NEW_METER_COLUMNS, "max_top_k")
POLICY_COLUMNS = (*CAPABILITY_COLUMNS, *NEW_METER_COLUMNS)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<v>.+)$""", re.M)


class _RecordingOp:
    """只记录加列 / 删列：本票是纯加列迁移，出现别的 op 就该先炸。"""

    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: Any) -> None:
        self.added.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str, **_kwargs: Any) -> None:
        self.dropped.append((table_name, column_name))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}：它只加列、只删列")


def _migration_path() -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{SLUG}.py"))
    assert len(matches) == 1, f"{SLUG}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration() -> ModuleType:
    path = _migration_path()
    spec = importlib.util.spec_from_file_location(f"t13_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _down_revision_of(path: Path) -> str | None:
    match = _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 down_revision"
    return match.group("v").strip().strip("'\"")


def _revision_of(path: Path) -> str:
    match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 revision"
    return match.group("revision")


def test_it_chains_onto_the_single_head() -> None:
    path = _migration_path()
    assert _down_revision_of(path) == PREVIOUS_HEAD
    paths = sorted(VERSIONS_DIR.glob("*.py"))
    revisions = {_revision_of(p): p for p in paths}
    pointed = {_down_revision_of(p) for p in paths} - {None}
    heads = set(revisions) - pointed
    # 链尾后来前移过：I 阶段的物化迁移接在 T1 之后，「API Key 只存哈希」再接在物化迁移之后
    # （历史迁移不改，新增一条）；第 16 步 #207 之后是 #214 的构建收敛四列。
    new_head_files = sorted(VERSIONS_DIR.glob("*_add_document_index_heartbeat.py"))
    assert len(new_head_files) == 1
    assert heads == {_revision_of(new_head_files[0])}, (
        f"链尾应当是「构建收敛四列」，实际 {sorted(heads)}"
    )


def test_upgrade_adds_every_new_column_and_touches_nothing_else() -> None:
    module = _load_migration()
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()

    added = [(table, column.name) for table, column in recorder.added]
    # 逐表逐列：plans 先（含只在它上面的 max_top_k），key_policies 后。
    expected = [("plans", name) for name in PLAN_COLUMNS] + [
        ("key_policies", name) for name in POLICY_COLUMNS
    ]
    assert set(added) == set(expected), "加列清单与 spec 不一致"
    assert len(added) == len(expected), "加列数量与 spec 不一致"
    assert recorder.dropped == [], "expand 段不许删任何旧列"


def test_every_new_column_is_not_null_with_a_server_default() -> None:
    module = _load_migration()
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()

    for table, column in recorder.added:
        assert column.nullable is False, f"{table}.{column.name} 必须非空"
        assert column.server_default is not None, f"{table}.{column.name} 需要回填缺省"
        if column.name in CAPABILITY_COLUMNS:
            assert column.type.python_type is bool
            # 基线能力回填 true，另两个新增加工位回填 false。
            expected = "true" if column.name == "retrieve_allowed" else "false"
            assert str(column.server_default.arg).lower() in (expected, f"{expected}()")
        else:
            # 整数列（四列计量 + max_top_k）缺省 0，此刻都不参与判定。
            assert column.type.python_type is int


def test_downgrade_drops_exactly_what_upgrade_added() -> None:
    module = _load_migration()
    recorder = _RecordingOp()
    module.op = recorder
    module.upgrade()
    added = [(table, column.name) for table, column in recorder.added]

    drop_recorder = _RecordingOp()
    module.op = drop_recorder
    module.downgrade()

    assert drop_recorder.added == []
    assert set(drop_recorder.dropped) == set(added), "回滚删列清单必须恰好等于本票加的列"
    assert len(drop_recorder.dropped) == len(added)
