"""第 13 步：套餐四档 free/plus/pro/max（standard 改名 plus，新增 max）

三件事一起做，因为它们都是同一次「套餐口径换挡」的后果：

* ``standard`` 原地改名为 ``plus``（租户只存整数 ``plan_id``，改名不破坏引用）；
* 插入第四档 ``max``；
* 把四档的**全部业务列**写成种子真值（五个授权位、三本账六格、``max_top_k``）。

第三件事不能省：上一版迁移只改了名字与 ``max_top_k``，于是存量库里
``rerank_qps`` / ``llm_qps`` 仍是加列时的 0。0 在新账里的语义是「没配额度 = no-op」，
升级上来的库会因此**永不计量重排与大模型**，对账（``preset_mismatches``）也会一直报差异。
这张迁移把它们一次写对，值取自 ``app.models.plan.PLAN_PRESETS``（逐项一致，由
``tests/test_four_tier_plan_migration.py`` 钉住）。

倍率口径：``plus`` 是基准（1×），``pro`` = 5×，``max`` = 20×；``free`` 只开基线
``retrieve`` 一个授权位，两本加工账都是 0。

**存量库安全**：只 UPDATE 命中的行 + ``INSERT ... WHERE NOT EXISTS``，重复升级不会重复插。
历史迁移不改（回放一致性），新迁移接在当前 head 之后。

Revision ID: d7e3b8a1f642
Revises: b4f6c1d90e27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7e3b8a1f642"
down_revision: Union[str, None] = "b4f6c1d90e27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 四档的真值（不含 ``code``，它由 WHERE 决定）。与 ``PLAN_PRESETS`` 逐项一致。
_TIER_VALUES: dict[str, dict[str, object]] = {
    "free": {
        "name": "免费版",
        "retrieve_allowed": True,
        "rewrite_allowed": False,
        "decompose_allowed": False,
        "research_allowed": False,
        "rerank_allowed": False,
        "retrieve_qps": 3,
        "retrieve_daily": 500,
        "rerank_qps": 0,
        "rerank_daily": 0,
        "llm_qps": 0,
        "llm_daily": 0,
        "max_top_k": 5,
        "max_kb": 1,
        "max_kb_per_retrieve": 1,
        "max_documents_per_kb": 30,
        "max_processing_documents": 1,
    },
    "plus": {
        "name": "进阶版",
        "retrieve_allowed": True,
        "rewrite_allowed": True,
        "decompose_allowed": True,
        "research_allowed": True,
        "rerank_allowed": True,
        "retrieve_qps": 10,
        "retrieve_daily": 5000,
        "rerank_qps": 3,
        "rerank_daily": 1000,
        "llm_qps": 1,
        "llm_daily": 300,
        "max_top_k": 10,
        "max_kb": 5,
        "max_kb_per_retrieve": 3,
        "max_documents_per_kb": 200,
        "max_processing_documents": 2,
    },
    "pro": {
        "name": "专业版",
        "retrieve_allowed": True,
        "rewrite_allowed": True,
        "decompose_allowed": True,
        "research_allowed": True,
        "rerank_allowed": True,
        "retrieve_qps": 50,
        "retrieve_daily": 25000,
        "rerank_qps": 15,
        "rerank_daily": 5000,
        "llm_qps": 5,
        "llm_daily": 1500,
        "max_top_k": 15,
        "max_kb": 50,
        "max_kb_per_retrieve": 5,
        "max_documents_per_kb": 5000,
        "max_processing_documents": 10,
    },
    "max": {
        "name": "旗舰版",
        "retrieve_allowed": True,
        "rewrite_allowed": True,
        "decompose_allowed": True,
        "research_allowed": True,
        "rerank_allowed": True,
        "retrieve_qps": 200,
        "retrieve_daily": 100000,
        "rerank_qps": 60,
        "rerank_daily": 20000,
        "llm_qps": 20,
        "llm_daily": 6000,
        "max_top_k": 20,
        "max_kb": 50,
        "max_kb_per_retrieve": 5,
        "max_documents_per_kb": 5000,
        "max_processing_documents": 10,
    },
}


def _sql_literal(value: object) -> str:
    """把迁移里的常量写成 SQL literal。

    值全部来自本文件顶部的字面量（不含任何外部输入），所以这里拼字符串是安全的；
    仍然走一遍规则而不是 ``f"{value}"``，是为了让布尔变成 ``TRUE``/``FALSE``
    （Python 的 ``True`` 不是合法的 PostgreSQL 布尔字面量）并转义单引号。
    """

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _assignments(values: dict[str, object]) -> str:
    return ", ".join(f"{column}={_sql_literal(value)}" for column, value in values.items())


def upgrade() -> None:
    # 1. standard → plus（先改名，后面的 UPDATE 才好按新 code 命中）。
    op.execute("UPDATE plans SET code='plus' WHERE code='standard'")

    # 2. 已存在的三档：把全部业务列写成种子真值。
    for code in ("free", "plus", "pro"):
        op.execute(f"UPDATE plans SET {_assignments(_TIER_VALUES[code])} WHERE code='{code}'")

    # 3. 插入 max（幂等：WHERE NOT EXISTS）。
    #
    # ``updated_at`` 必须显式给 ``now()``：它是 ``NOT NULL`` 且**没有 server_default**
    # （默认值在 ORM 的 Python 侧，``app/models/plan.py``）。裸 INSERT 绕过 ORM，漏了它
    # 就是 NotNullViolation——而单测只断言 SQL 字符串形状，看不见这件事，只有真库能看见。
    values = {"code": "max", **_TIER_VALUES["max"]}
    columns = ", ".join(values)
    literals = ", ".join(_sql_literal(value) for value in values.values())
    op.execute(
        f"INSERT INTO plans ({columns}, updated_at) SELECT {literals}, now() "
        "WHERE NOT EXISTS (SELECT 1 FROM plans WHERE code='max')"
    )


def downgrade() -> None:
    """回到三档。

    只能回到**形状**，回不到旧数值：升级时把四档的业务列统一改写成了种子真值，旧值没有
    留底。降级因此把 plus 改回 standard、删掉 max、把 ``max_top_k`` 复位 0（= 不额外
    封顶），额度列保持原样——擅自把它们猜回某个值，比留着当前值更容易造成事故。
    """

    op.execute("DELETE FROM plans WHERE code='max'")
    op.execute("UPDATE plans SET code='standard', name='标准版', max_top_k=0 WHERE code='plus'")
    op.execute("UPDATE plans SET max_top_k=0 WHERE code IN ('free', 'pro')")
