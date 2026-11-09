"""create accounts

控制台账号。与租户分离：平台管理员跨租户管账号与套餐，它不属于任何租户，所以
``tenant_id`` 可空——若把管理员投到某个「平台租户」名下，那个假租户就凭空拥有了一套
数据面身份。

``username`` 唯一（登录靠它定位账号）；``password_hash`` 宽 255 是给 bcrypt / argon2
留的余量，明文口令一旦入库就是全量泄露，没有补救手段。

Revision ID: 9b3e4c1f6d82
Revises: 8a2d3b0e5c71
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3e4c1f6d82"
down_revision: Union[str, None] = "8a2d3b0e5c71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index(op.f("ix_accounts_tenant_id"), "accounts", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_accounts_tenant_id"), table_name="accounts")
    op.drop_table("accounts")
