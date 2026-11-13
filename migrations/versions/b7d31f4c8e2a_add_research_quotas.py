"""add research quotas（``plans`` 与 ``key_policies`` 各加 ``research_qps`` / ``research_daily``）

受控 Research（第 11 步 C，票 #150）要一份**独立**的速率与日配额：一次 Research 约 30 秒、
3 次模型调用，让它与普通检索共享同一本账，等于用一次重资源调用吃掉一个租户当天的检索额度。
所以两列按检索那两列**同构**地加（两级同记：Key 级卡单个应用、租户级聚合），但计数桶与键
前缀都是另一本账。

| 表 | 列 | 回答什么 |
| --- | --- | --- |
| ``plans`` | ``research_qps`` / ``research_daily`` | 这个租户买了多少（种子：pro 1 / 100，free 与 standard 0） |
| ``key_policies`` | 同两列 | 这套集成能用多少（新 Key 默认 0 = 关闭） |

**0 是「关闭」，不是「额度填错」**——这与 ``retrieve_qps=0`` 的语义刻意相反（那条等于当场
停服）。门禁把「配额为 0」与「能力开关为假」答成同一个码（``20013``），不泄露配置细节。

四列都 ``NOT NULL`` + ``server_default 0``：**既有行在升级那一刻就被回填成 0**（PostgreSQL
加列时按 server_default 填充存量行），于是「升级完的库」与「改造前的库」跑出同一个结果——
Research 仍然关着，要开就由控制面板显式打开（那正是本票的另一半）。这条与 #146 那条加开关
的迁移同一个精神：加能力不许悄悄改变现有行为。

``downgrade`` 干净删掉这四列：回滚后剩下的就是改造前那两张表，不留半列。

Revision ID: b7d31f4c8e2a
Revises: e41b7c9d2a63
Create Date: 2026-11-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d31f4c8e2a"
down_revision: Union[str, None] = "e41b7c9d2a63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 四列：两张表各两列。名字相同是刻意的——它们回答的是同两个问题（每秒几次、每天几次），
#: 只是所属层级不同（套餐 / 密钥配置），改一处名字就等于让两级计数长得不一样。
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plans", "research_qps"),
    ("plans", "research_daily"),
    ("key_policies", "research_qps"),
    ("key_policies", "research_daily"),
)


def upgrade() -> None:
    for table_name, column_name in _COLUMNS:
        op.add_column(
            table_name,
            sa.Column(column_name, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for table_name, column_name in _COLUMNS:
        op.drop_column(table_name, column_name)
