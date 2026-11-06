"""add documents.index_claimed_at

Revision ID: b71a4c5e2f90
Revises: d657b09304ca
Create Date: 2026-11-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b71a4c5e2f90'
down_revision: Union[str, None] = 'd657b09304ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 索引租约时间：区分「已入队未开工」（NULL）与「已被抢占、正在跑」（有时间戳）。
    op.add_column("documents", sa.Column("index_claimed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "index_claimed_at")
