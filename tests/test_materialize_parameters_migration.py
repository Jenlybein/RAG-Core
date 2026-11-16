"""第 13 步 I 阶段：物理删档位存储迁移的结构验收。

不连真库：把 op 换成记录器，验证升级加三列 + 删七列/一表、降级反向重建。
链头断言钉在历史迁移上（迁移链每次新增都要人确认 head）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
SLUG = "b4f6c1d90e27_materialize_retrieval_parameters"
REVISION = "b4f6c1d90e27"
PREVIOUS = "a1c4e7b92d60"

_REV = re.compile(r'^revision(?::\s*str)?\s*=\s*[\'"]([^\'"]+)[\'"]', re.M)
_DOWN = re.compile(r"^down_revision(?::\s*[^=]+)?\s*=\s*(.+)$", re.M)


class _Recorder:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []
        self.dropped: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []
        self.created_tables: list[str] = []
        self.executed: list[str] = []

    def add_column(self, table: str, column: Any) -> None:
        self.added.append((table, column.name))

    def drop_column(self, table: str, column: str, **_k: Any) -> None:
        self.dropped.append((table, column))

    def drop_table(self, table: str) -> None:
        self.dropped_tables.append(table)

    def create_table(self, name: str, *_a: Any, **_k: Any) -> None:
        self.created_tables.append(name)

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"迁移不该用到 op.{name}")


def _load() -> ModuleType:
    path = VERSIONS / f"{SLUG}.py"
    spec = importlib.util.spec_from_file_location(f"t13_i_{REVISION}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_migration_chains_onto_the_single_head() -> None:
    revisions: dict[str, str] = {}
    downs: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = _REV.search(text)
        down = _DOWN.search(text)
        if rev:
            revisions[rev.group(1)] = path.name
        if down and not down.group(1).strip().startswith("None"):
            value = re.search(r'[\'"]([^\'"]+)[\'"]', down.group(1))
            if value:
                downs.add(value.group(1))
    heads = set(revisions) - downs
    assert heads == {REVISION}
    module = _load()
    assert module.down_revision == PREVIOUS


def test_upgrade_adds_three_columns_drops_old_columns_and_table() -> None:
    module = _load()
    rec = _Recorder()
    module.op = rec
    module.upgrade()

    assert {name for table, name in rec.added if table == "key_policies"} == {
        "retrieval_mode",
        "retrieval_top_k",
        "retrieval_rerank",
    }
    assert ("plans", "allowed_profiles") in rec.dropped
    assert ("key_policies", "profile") in rec.dropped
    assert ("key_policies", "custom_options") in rec.dropped
    assert ("plans", "research_qps") in rec.dropped
    assert rec.dropped_tables == ["profile_presets"]
    assert len(rec.executed) == 1, "custom_options → 真列的数据搬迁必须发生"


def test_downgrade_rebuilds_table_columns_and_writes_back() -> None:
    module = _load()
    rec = _Recorder()
    module.op = rec
    module.downgrade()

    assert "profile_presets" in rec.created_tables
    assert ("key_policies", "custom_options") in rec.added
    assert ("key_policies", "retrieval_mode") in rec.dropped
    assert any("jsonb_build_object" in sql for sql in rec.executed)
