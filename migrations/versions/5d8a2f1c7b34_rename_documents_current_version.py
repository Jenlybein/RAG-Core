"""rename documents.doc_version to current_version

「第几稿」与「当前稿指针」在代码里长期共用一个名字 ``doc_version``，于是「文档行上的
指针」和「切片所属的稿次」两个意思互相污染。本步只做改名：``documents`` 上那一列从
``doc_version`` 改成 ``current_version``（当前稿指针），对外响应字段名与切片 meta 里的
``doc_version``（稿号）都**不动**——它们说的是另一种东西。

纯改名，没有数据回填：``alter_column(new_column_name=...)`` 在库上是一条
``ALTER TABLE ... RENAME COLUMN``，存量值、NOT NULL 与默认值 1 都跟着列走。
``downgrade`` 对称地改回去。

Revision ID: 5d8a2f1c7b34
Revises: f2b8d4a6c913
Create Date: 2026-11-15 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5d8a2f1c7b34'
down_revision: Union[str, None] = 'f2b8d4a6c913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 只改名：默认值、NOT NULL 与存量值都挂在同一列上，不重建、不回填。
    op.alter_column(
        'documents',
        'doc_version',
        existing_type=sa.Integer(),
        new_column_name='current_version',
    )


def downgrade() -> None:
    op.alter_column(
        'documents',
        'current_version',
        existing_type=sa.Integer(),
        new_column_name='doc_version',
    )
