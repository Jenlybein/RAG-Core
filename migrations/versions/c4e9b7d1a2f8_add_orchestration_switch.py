"""add orchestration switch（三层：套餐允许 / 密钥配置启用 / 档位想要）

切片编排（第 11 步票 #146）是「先把开关装好，再让功能长进来」：B2 的编排服务落地时，
开关的判定路径已经被用例钉住了。所以这条迁移只加三列，**不改任何行为**：

| 表 | 列 | 三层里回答什么 |
| --- | --- | --- |
| ``plans`` | ``orchestration_allowed`` | 这个租户买没买（种子：free / standard 否，pro 是） |
| ``key_policies`` | ``orchestration_allowed`` | 这套集成要不要（默认关） |
| ``profile_presets`` | ``orchestration`` | 这一档平时的默认（四行初始都关） |

三列都 ``NOT NULL`` + ``server_default false``：**既有行在升级那一刻就被回填成
``False``**（PostgreSQL 加列时按 server_default 填充存量行），于是「升级完的库」与
「改造前的库」跑出同一个结果——默认关闭就是这次迁移要保住的现有行为。四项计数
（知识库 / 切片 / 向量 / 关键词文档）不参与，也不该被这条迁移碰到。

``downgrade`` 干净删掉这三列：回滚后剩下的就是改造前那三张表，不留半列。

Revision ID: c4e9b7d1a2f8
Revises: a91f3c7e2058
Create Date: 2026-11-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e9b7d1a2f8"
down_revision: Union[str, None] = "a91f3c7e2058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 三个 ``(表, 列)``。三层的列名刻意不同：套餐 / 配置是「允许 / 启用」，档位是「想要」。
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plans", "orchestration_allowed"),
    ("key_policies", "orchestration_allowed"),
    ("profile_presets", "orchestration"),
)


def upgrade() -> None:
    for table_name, column_name in _COLUMNS:
        op.add_column(
            table_name,
            sa.Column(column_name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    for table_name, column_name in _COLUMNS:
        op.drop_column(table_name, column_name)
