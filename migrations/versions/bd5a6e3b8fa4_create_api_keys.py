"""create api_keys

数据面唯一的凭证。``key_value`` 加唯一索引：鉴权路径就是「拿原文一击命中」，多一条
索引都嫌多；``tenant_id`` 与 ``policy_id`` 各加普通索引，是因为控制台与配额计数都按
这两列读。

本期 ``key_value`` 存**明文**（ADR-0004 的明确取舍：调试台要拿原文打真实接口）。
列宽留到 64 而不是掐着 40：生产改成「哈希 + 密文」双列时，密文会长出这里。

Revision ID: bd5a6e3b8fa4
Revises: ac4f5d2a7e93
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd5a6e3b8fa4"
down_revision: Union[str, None] = "ac4f5d2a7e93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("key_value", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_api_keys_policy_id"), "api_keys", ["policy_id"], unique=False)
    op.create_index(op.f("ix_api_keys_key_value"), "api_keys", ["key_value"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_api_keys_key_value"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_policy_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_tenant_id"), table_name="api_keys")
    op.drop_table("api_keys")
