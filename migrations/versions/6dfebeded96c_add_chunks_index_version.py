"""add chunks.index_version

索引版本：切片在写入时带上版本号，默认 v1。本步只引入列与索引，不引入版本
生命周期（重建仍是先删后写，同一知识库同时只存在一个索引版本）。

复合索引 (tenant_id, kb_id, index_version) 供后续的范围检索使用。

Revision ID: 6dfebeded96c
Revises: b71a4c5e2f90
Create Date: 2026-11-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6dfebeded96c'
down_revision: Union[str, None] = 'b71a4c5e2f90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 让存量行在加列时直接拿到 v1，列因此可以 NOT NULL。
    op.add_column(
        "chunks",
        sa.Column(
            "index_version",
            sa.String(length=32),
            nullable=False,
            server_default="v1",
        ),
    )
    op.create_index(
        "ix_chunks_tenant_id_kb_id_index_version",
        "chunks",
        ["tenant_id", "kb_id", "index_version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_tenant_id_kb_id_index_version", table_name="chunks")
    op.drop_column("chunks", "index_version")
