"""模型角色指针合并迁移的结构验收（不连真库，用 op 记录器）。

病根：第 13 步把角色从 ``fast`` / ``strong`` 并成 ``default``，但没有迁移搬那两条指针。
升级后的部署于是「库里配好了模型接入，运行时却按 ``.env`` 打」——回落不报错，症状只是
「配的那条好像没生效」。真实踩到过一次 403 ``AccountOverdueError``。

这里钉住三件事：

1. 迁移挂在四档套餐那条之后（链不能断）；
2. 升级语句**只在新键缺失时**插入，且优先取 ``fast``（``ORDER BY key LIMIT 1`` 让结果
   确定，不依赖数据库返回行的顺序）；
3. 两条撤销的键被删掉——它们留在库里只会制造「配了但没用」的错觉。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
SLUG = "a4c81f60d9b2_merge_model_role_pointers"


class _Recorder:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: object) -> None:
        self.executed.append(" ".join(str(sql).split()))

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"迁移不该用到 op.{name}")


def _load() -> ModuleType:
    path = VERSIONS / f"{SLUG}.py"
    spec = importlib.util.spec_from_file_location("t13_model_roles", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run() -> list[str]:
    module = _load()
    recorder = _Recorder()
    original = sys.modules[module.__name__]
    original.op = recorder  # type: ignore[attr-defined]
    module.upgrade()
    return recorder.executed


def test_migration_chains_onto_four_tier_plans() -> None:
    module = _load()
    assert module.revision == "a4c81f60d9b2"
    assert module.down_revision == "d7e3b8a1f642"


def test_upgrade_merges_pointers_before_dropping_them() -> None:
    statements = _run()

    assert len(statements) == 2
    insert, delete = statements

    # 先插后删：顺序反了会把唯一的来源删掉，default 就永远建不出来。
    assert insert.startswith("INSERT INTO runtime_settings")
    assert "model.role.default" in insert
    assert delete.startswith("DELETE FROM runtime_settings")


def test_insert_does_not_overwrite_an_existing_default() -> None:
    insert = _run()[0]

    assert "NOT EXISTS" in insert
    assert "WHERE key = 'model.role.default'" in insert


def test_insert_picks_one_source_deterministically() -> None:
    insert = _run()[0]

    # 两条旧指针都存在时不能插两行：PK 会冲突，而且「搬哪一条」必须可预测。
    assert "ORDER BY legacy.key" in insert
    assert "LIMIT 1" in insert


def test_insert_skips_an_empty_pointer() -> None:
    insert = _run()[0]

    # 空串指针 = 「不指向任何一条」。把它搬成 default 等于凭空造一个空指向。
    assert """<> '\"\"'::jsonb""" in insert


def test_delete_removes_exactly_the_retired_keys() -> None:
    delete = _run()[1]

    assert "model.role.fast" in delete
    assert "model.role.strong" in delete
    assert "model.role.default" not in delete


def test_downgrade_is_explicitly_not_implemented() -> None:
    module = _load()

    assert "故意" in module.downgrade.__doc__ or "不回" in module.downgrade.__doc__
    # 反向搬回去会凭空造出代码里已经不存在的两个角色，所以这里必须是空实现。
    recorder = _Recorder()
    module.op = recorder  # type: ignore[attr-defined]
    module.downgrade()
    assert recorder.executed == []
