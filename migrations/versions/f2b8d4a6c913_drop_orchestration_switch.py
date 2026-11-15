"""drop orchestration switch（撤销「套餐允许 / 密钥配置启用 / 档位想要」三层）

ADR-0029 决策二撤销了独立编排块（#138），所以先把开关装进去的那三列也要跟着走：
一个有列、没人读的能力，是它最容易重新长回来的土壤。

| 表 | 列 | 三层里曾经回答什么 |
| --- | --- | --- |
| ``plans`` | ``orchestration_allowed`` | 这个租户买没买（种子：free / standard 否，pro 是） |
| ``key_policies`` | ``orchestration_allowed`` | 这套集成要不要（默认关） |
| ``profile_presets`` | ``orchestration`` | 这一档平时的默认（四行初始都关） |

``upgrade`` 只删这三列，不碰别的：存量行的值没有第二处可搬——能力没了，「谁开过」
这件事只能去 ``audit_logs`` 里查（面板改开关时写过账本）。

``downgrade`` 重建三列，``NOT NULL`` + ``server_default false``：**回填 ``false``**，
于是回滚后得到的是「开关装好了、一个都没开」的那张库，与 ``add_orchestration_switch``
跑完那一刻逐列相同。回填 ``true`` 会让一次回滚悄悄把能力打开——那是回滚不该做的事。

分成两条迁移而不是改历史：``add_orchestration_switch``（c4e9b7d1a2f8）已经在别人的库上
跑过了，改它等于让同一个 revision id 在不同机器上代表两件不同的事。

Revision ID: f2b8d4a6c913
Revises: e8d4a1c73b52（#138 的删表迁移；两条迁移原本都从 b3d7e1a95c24 分叉，
合并时串成一条链，否则会出现两个 head）
Create Date: 2026-11-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b8d4a6c913"
down_revision: Union[str, None] = "e8d4a1c73b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 三个 ``(表, 列)``。与 ``add_orchestration_switch`` 的 ``_COLUMNS`` 逐字对应、方向相反
#: ——两份清单互为逆操作这件事由用例钉住。
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plans", "orchestration_allowed"),
    ("key_policies", "orchestration_allowed"),
    ("profile_presets", "orchestration"),
)


def upgrade() -> None:
    for table_name, column_name in _COLUMNS:
        op.drop_column(table_name, column_name)


def downgrade() -> None:
    for table_name, column_name in _COLUMNS:
        op.add_column(
            table_name,
            sa.Column(column_name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
