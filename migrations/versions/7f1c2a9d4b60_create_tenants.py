"""create tenants

身份五表的第一张。此前「租户」只是四张业务表上的一列字符串，没有任何一行数据能表达
「这是一家客户、它买了哪一档、它还开不开着」——这些都属于请求之外的管理事实。

一表一迁移：回滚到这一步只会带走 ``tenants``，不会顺手动到套餐或账号；五张表之间的
引用关系由应用层校验，所以建表顺序可以按「先有谁」排，而不必迁就外键。

``plan_id`` 是非空整数且**不加外键**：租户没有套餐就没有任何上限来源，这不是合法
状态；而不加外键与既有四张表保持一致，避免库里有两套引用风格。

Revision ID: 7f1c2a9d4b60
Revises: cecd88875297
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f1c2a9d4b60"
down_revision: Union[str, None] = "cecd88875297"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("tenants")
