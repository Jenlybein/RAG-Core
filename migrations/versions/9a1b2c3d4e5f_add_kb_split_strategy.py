"""add split_strategy to knowledge_bases

切块策略是**知识库级**属性，不是文档级：同一个库里混用两种切法，片长分布会不一致
（按结构切约 150 字、按 size 切约 725 字），检索时的向量相似度会被「片长」这个与
内容无关的因素污染。所以策略放在 knowledge_bases 上，建库时定一次。

`chunk_size` 不进独立列，而是放在已有的 `settings` JSONB 里：
它是数值旋钮，靠应用层做范围校验就够了，不值得为它加一次迁移；
`split_strategy` 是枚举，值写错的后果是「检索悄悄变差」而不是报错，所以值得
拥有独立列外加数据库 CHECK 约束。

Revision ID: 9a1b2c3d4e5f
Revises: 6dfebeded96c
Create Date: 2026-11-08 22:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9a1b2c3d4e5f'
down_revision: Union[str, None] = '6dfebeded96c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 是必需的：表里已有存量行，没有默认值就加不了 NOT NULL 列。
    # 存量行按 structure 解释——这是本步之前唯一的行为。
    op.add_column(
        'knowledge_bases',
        sa.Column(
            'split_strategy',
            sa.String(length=32),
            nullable=False,
            server_default='structure',
        ),
    )
    op.create_check_constraint(
        'ck_knowledge_bases_split_strategy',
        'knowledge_bases',
        "split_strategy IN ('size', 'structure')",
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_knowledge_bases_split_strategy', 'knowledge_bases', type_='check'
    )
    op.drop_column('knowledge_bases', 'split_strategy')
