"""第 13 步四档套餐迁移的结构验收（不连真库，用 op 记录器）。

升级：standard→plus 改名 + 四档 max_top_k + 幂等插入 max；
降级：删 max、plus 改回 standard、max_top_k 复位。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
SLUG = "d7e3b8a1f642_rename_standard_to_plus_and_add_max"


class _Recorder:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: object) -> None:
        self.executed.append(str(sql))

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"迁移不该用到 op.{name}")


def _load() -> ModuleType:
    path = VERSIONS / f"{SLUG}.py"
    spec = importlib.util.spec_from_file_location("t13_four_tier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_migration_chains_onto_materialize() -> None:
    module = _load()
    assert module.revision == "d7e3b8a1f642"
    assert module.down_revision == "b4f6c1d90e27"


def test_upgrade_renames_standard_sets_max_top_k_and_inserts_max() -> None:
    module = _load()
    rec = _Recorder()
    module.op = rec
    module.upgrade()

    joined = "\n".join(rec.executed)
    assert "code='plus'" in joined and "code='standard'" in joined
    assert "max_top_k=10" in joined  # plus 的改名语句
    assert "max_top_k=20" in joined or "20, 200, 100000" in joined  # max 的 INSERT 值
    assert "INSERT INTO plans" in joined and "NOT EXISTS" in joined
    assert "code='max'" in joined


def test_downgrade_reverts_everything() -> None:
    module = _load()
    rec = _Recorder()
    module.op = rec
    module.downgrade()

    joined = "\n".join(rec.executed)
    assert "DELETE FROM plans WHERE code='max'" in joined
    assert "code='standard'" in joined
    assert "max_top_k=0" in joined


def test_only_one_head() -> None:
    rev = re.compile(r'^revision(?::\s*str)?\s*=\s*[\'"]([^\'"]+)[\'"]', re.M)
    down = re.compile(r"^down_revision(?::\s*[^=]+)?\s*=\s*(.+)$", re.M)
    revisions: dict[str, str] = {}
    downs: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        m = rev.search(text)
        d = down.search(text)
        if m:
            revisions[m.group(1)] = path.name
        if d and not d.group(1).strip().startswith("None"):
            v = re.search(r'[\'"]([^\'"]+)[\'"]', d.group(1))
            if v:
                downs.add(v.group(1))
    assert set(revisions) - downs == {"d7e3b8a1f642"}
