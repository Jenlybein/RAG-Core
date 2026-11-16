"""第 13 步：套餐四档 free/plus/pro/max（standard 改名 plus，新增 max）

把现存的 ``standard`` 行原地改名为 ``plus``（租户只存整数 plan_id，改名不破坏引用），
并插入第四档 ``max``。四档 ``max_top_k`` 分别设为 5/10/15/20，与 TIER_SEEDS 与
``PLAN_PRESETS`` 一致；max 的其它数值沿用 pro（只在库里比 pro 多一份规模）。

**存量库安全**：只 UPDATE 命中的行 + INSERT ... WHERE NOT EXISTS，重复升级不会重复插。
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

# max 行的真实列值（与 app.models.plan.PLAN_PRESETS[PLAN_MAX] 逐项一致）。
_MAX_VALUES: dict[str, object] = {
    "code": "max",
    "name": "旗舰版",
    "rerank_allowed": True,
    "research_allowed": True,
    "retrieve_allowed": True,
    "rewrite_allowed": True,
    "decompose_allowed": True,
    "rerank_qps": 0,
    "rerank_daily": 0,
    "llm_qps": 0,
    "llm_daily": 0,
    "max_top_k": 20,
    "retrieve_qps": 200,
    "retrieve_daily": 100000,
    "max_kb": 50,
    "max_kb_per_retrieve": 5,
    "max_documents_per_kb": 5000,
    "max_processing_documents": 10,
}


def upgrade() -> None:
    # 1. standard → plus（含展示名），并把四档 max_top_k 落到种子值。
    op.execute("UPDATE plans SET code='plus', name='进阶版', max_top_k=10 WHERE code='standard'")
    op.execute("UPDATE plans SET name='免费版', max_top_k=5 WHERE code='free'")
    op.execute("UPDATE plans SET name='专业版', max_top_k=15 WHERE code='pro'")

    # 2. 插入 max（幂等：WHERE NOT EXISTS）。
    columns = ", ".join(_MAX_VALUES)
    literals = ", ".join(_sql_literal(value) for value in _MAX_VALUES.values())
    op.execute(
        f"INSERT INTO plans ({columns}) SELECT {literals} "
        "WHERE NOT EXISTS (SELECT 1 FROM plans WHERE code='max')"
    )


def _sql_literal(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def downgrade() -> None:
    # 删除 max、plus 改回 standard，max_top_k 复位 0。
    op.execute("DELETE FROM plans WHERE code='max'")
    op.execute("UPDATE plans SET code='standard', name='标准版', max_top_k=0 WHERE code='plus'")
    op.execute("UPDATE plans SET max_top_k=0 WHERE code IN ('free', 'pro')")
