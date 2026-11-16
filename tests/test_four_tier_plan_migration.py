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
    assert "INSERT INTO plans" in joined and "NOT EXISTS" in joined
    assert "code='max'" in joined
    # max 的 INSERT：列名与 SELECT 字面量按位对齐，max_top_k 那一列必须是 20。
    insert = next(sql for sql in rec.executed if sql.startswith("INSERT INTO plans"))
    columns = insert[insert.index("(") + 1 : insert.index(")")].split(", ")
    literals = insert.split("SELECT ", 1)[1].split(" WHERE", 1)[0].split(", ")
    assert dict(zip(columns, literals))["max_top_k"] == "20"


def test_the_insert_covers_every_column_that_has_no_default() -> None:
    """`max` 那行 INSERT 必须覆盖**所有 NOT NULL 且无默认值**的列。

    裸 SQL 绕过 ORM，而 ORM 的 ``default=`` 是**Python 侧**的——数据库里并没有默认值。
    漏一列就是 `NotNullViolation`，而「只断言 SQL 字符串形状」的用例看不见它：真库一跑
    就炸（本步实测踩到，漏的是 ``updated_at``）。这条用例把「哪些列必须给」从当前模型
    推出来，而不是抄一份列名清单。
    """

    from app.models.plan import Plan

    required = {
        column.name
        for column in Plan.__table__.columns
        if not column.nullable
        and column.server_default is None
        and not column.primary_key  # id 由序列给值
    }

    module = _load()
    rec = _Recorder()
    module.op = rec
    module.upgrade()

    insert = next(sql for sql in rec.executed if sql.startswith("INSERT INTO plans"))
    columns = {
        name.strip() for name in insert[insert.index("(") + 1 : insert.index(")")].split(",")
    }
    assert required <= columns, f"INSERT 漏了这些必填列：{sorted(required - columns)}"


def test_downgrade_reverts_everything() -> None:
    module = _load()
    rec = _Recorder()
    module.op = rec
    module.downgrade()

    joined = "\n".join(rec.executed)
    assert "DELETE FROM plans WHERE code='max'" in joined
    assert "code='standard'" in joined
    assert "max_top_k=0" in joined


def test_the_migration_values_are_exactly_the_seed_presets() -> None:
    """迁移里的四档数值必须与 ``PLAN_PRESETS`` 逐列相等。

    迁移不能 import 应用代码（历史脚本要能在当时的代码上跑），所以 ``_TIER_VALUES``
    是四档数值的**第二份拷贝**。拷贝本身是必要的，没有护栏才是问题：种子改了而迁移没改，
    新库播出来的行与旧库迁出来的行就会不一样，而这种偏差只会在生产环境里显形。
    这条用例就是那道护栏。
    """

    from app.models.plan import PLAN_BUSINESS_COLUMNS, PLAN_PRESETS

    module = _load()
    assert set(module._TIER_VALUES) == set(PLAN_PRESETS), "四档的 code 集合必须一致"
    for code, values in module._TIER_VALUES.items():
        # 迁移表比业务列多一个 ``name``（展示名也要跟着改），除此之外必须一一对应。
        assert set(values) == {*PLAN_BUSINESS_COLUMNS, "name"}, f"{code}: 列集合必须完整"
        assert values["name"] == PLAN_PRESETS[code].name, f"{code}.name"
        for column in PLAN_BUSINESS_COLUMNS:
            assert values[column] == getattr(PLAN_PRESETS[code], column), f"{code}.{column}"


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
