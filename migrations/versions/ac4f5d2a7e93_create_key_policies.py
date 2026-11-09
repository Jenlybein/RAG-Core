"""create key_policies

密钥配置独立成表：一套配置回答「这套集成能用多好、能用多少」，它属于租户、租户内
可复用，删除时引用它的 Key 字符串级失效。若把这些列摊进 ``api_keys``，租户内换一把
Key 就得重建一遍配置，「改一处全生效」与「删配置即失效」都无处落脚。

「档位所需能力 ⊆ 配置开关 ⊆ 套餐能力」这三层包含不在列上做约束：CHECK 写不出跨表的
包含关系，写得出的那部分也会把错误信息变成数据库报错，而不是一条可读的参数错误。

Revision ID: ac4f5d2a7e93
Revises: 9b3e4c1f6d82
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ac4f5d2a7e93"
down_revision: Union[str, None] = "9b3e4c1f6d82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "key_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "allowed_kb_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("research_allowed", sa.Boolean(), nullable=False),
        sa.Column("hybrid_allowed", sa.Boolean(), nullable=False),
        sa.Column("rerank_allowed", sa.Boolean(), nullable=False),
        sa.Column("query_rewrite_allowed", sa.Boolean(), nullable=False),
        sa.Column("retrieve_qps", sa.Integer(), nullable=False),
        sa.Column("retrieve_daily", sa.Integer(), nullable=False),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("include_parent", sa.Boolean(), nullable=False),
        sa.Column("expand_table_rows", sa.Boolean(), nullable=False),
        sa.Column(
            "custom_options",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_key_policies_tenant_id"), "key_policies", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_key_policies_tenant_id"), table_name="key_policies")
    op.drop_table("key_policies")
