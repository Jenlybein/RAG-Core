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
        # 后来才加的列由后续迁移补（都把 server_default 写进了库），不归这条历史迁移。
        and column.name not in _ADDED_AFTER_MIGRATION
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


#: 历史迁移之后**又被删掉**的套餐列。历史迁移不许改（它要能在当时那份代码上跑），
#: 而护栏用例要在「迁移表 vs 现在的种子」之间对齐，所以把已删列显式列出来。
_REMOVED_PLAN_COLUMNS = {
    # #218：并发处理名额（ADR-0034 决策八）。
    "max_processing_documents",
}

#: **后来被后续迁移改过值**的列：历史迁移里的数字是当时的真相，而种子已经按 ADR 改过了。
#: 逐列比对对这些列不成立（它们由新迁移负责对齐）。
_RECONCILED_AFTER_MIGRATION = {
    # #220：ADR-0035 决策六把免费档 1→3、进阶档 5→10、旗舰档 50→100。
    "max_kb",
}

#: **后来才加**的套餐列：它们由后续迁移（带 server_default）补上，不由这条历史迁移的
#: 裸 SQL 写入。两条护栏（「INSERT 覆盖必填列」与「迁移表逐列对账」）都必须把它们排除，
#: 否则它们会让一条**本来正确**的历史迁移看起来是错的。
_ADDED_AFTER_MIGRATION = {
    # #220 / #222：单库 token 容量与每日切块额度（ADR-0035）。
    "max_tokens_per_kb",
    "chunk_daily_tokens",
}


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
        # 迁移表比业务列多两类东西：``name``（展示名也要跟着改），以及**历史迁移之后又被
        # 删掉的列**（``max_processing_documents`` 随 #218 删除，但历史迁移不许改——它要能
        # 在当时那份代码上跑）。所以这里的集合是「业务列 + 已删列 + name」，而逐列比对只
        # 对**还在**的业务列做：已删列没有可比对的种子值了。
        # 历史迁移表 = **当时**的业务列 + 已删列 + name；后来才加的列不在其中。
        assert set(values) == {
            *(set(PLAN_BUSINESS_COLUMNS) - _ADDED_AFTER_MIGRATION),
            *_REMOVED_PLAN_COLUMNS,
            "name",
        }, f"{code}: 列集合必须完整"
        assert values["name"] == PLAN_PRESETS[code].name, f"{code}.name"
        for column in PLAN_BUSINESS_COLUMNS:
            if column in _RECONCILED_AFTER_MIGRATION or column in _ADDED_AFTER_MIGRATION:
                # 被后续迁移改过 / 后来才加的列由那些迁移负责对齐（见 ``d5f9c2a7b3e8`` /
                # ``c3d6e9f2b5a8``）：历史迁移表里根本没有它们。
                continue
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
    assert len(set(revisions) - downs) == 1
