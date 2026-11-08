"""add doc_version to documents

「这是第几稿」与「这是第几次重建算出来的」（``index_version``）是两个问题，
ADR-0005 已把两者定成正交并存。本步只做前半：给 documents 加稿号并随切片落库，
**不建 doc_versions 表，也不加「默认只查当前稿」的 SQL/ES 约束**——那整套留给
第 10 步「文档版本管理」。

所以这一列现在的唯一作用是让引用三元组（文档 + 稿次 + 切片）里有稿次可写；
读取侧一律不过滤。谁要按稿号过滤，必须先实现 doc_versions 与约束（ADR-0005）。

Revision ID: 4c7e8f9a0b1c
Revises: 9a1b2c3d4e5f
Create Date: 2026-11-08 22:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4c7e8f9a0b1c'
down_revision: Union[str, None] = '9a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 存量行按第 1 稿解释：它们只有一份内容，这是唯一合理的假设。
    op.add_column(
        'documents',
        sa.Column('doc_version', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    op.drop_column('documents', 'doc_version')
