"""create plans

套餐定义进库。此前上限散在代码常量里：运营想给一家客户临时提额度，只能改代码发版，
改之前没法预览影响面，改之后也没有「谁把什么改成了什么」。

11 个业务列全部落在表里，一列都不留常量——面板上会出现输入框的字段必须能读能写，
「界面上改得动、后端读不到」比「界面上根本没有这个字段」更糟。本文件里的三档种子由
播种逻辑写入，只在表为空时执行。

``code`` 唯一索引：脚本与日志都用它指代一档（``free`` / ``standard`` / ``pro``）。

Revision ID: 8a2d3b0e5c71
Revises: 7f1c2a9d4b60
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8a2d3b0e5c71"
down_revision: Union[str, None] = "7f1c2a9d4b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "allowed_profiles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("hybrid_allowed", sa.Boolean(), nullable=False),
        sa.Column("rerank_allowed", sa.Boolean(), nullable=False),
        sa.Column("query_rewrite_allowed", sa.Boolean(), nullable=False),
        sa.Column("research_allowed", sa.Boolean(), nullable=False),
        sa.Column("retrieve_qps", sa.Integer(), nullable=False),
        sa.Column("retrieve_daily", sa.Integer(), nullable=False),
        sa.Column("max_kb", sa.Integer(), nullable=False),
        sa.Column("max_kb_per_retrieve", sa.Integer(), nullable=False),
        sa.Column("max_documents_per_kb", sa.Integer(), nullable=False),
        sa.Column("max_processing_documents", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_plans_code"), "plans", ["code"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_plans_code"), table_name="plans")
    op.drop_table("plans")
