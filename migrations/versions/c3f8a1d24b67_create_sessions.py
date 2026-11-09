"""create sessions

控制台的服务端会话：一行 = 一次登录。主键就是随机的不可猜会话标识（凭证本身），
``account_id`` 加普通索引（登出与排障都按它读）。``expires_at`` 是硬过期时间——停用
账号靠「每次请求回账号表现读」立刻生效，不靠这张表的过期时间。

与身份五表同一风格：裸字符串 id、不加外键、不建关系。

Revision ID: c3f8a1d24b67
Revises: bd5a6e3b8fa4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f8a1d24b67"
down_revision: Union[str, None] = "bd5a6e3b8fa4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_account_id"), "sessions", ["account_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sessions_account_id"), table_name="sessions")
    op.drop_table("sessions")
