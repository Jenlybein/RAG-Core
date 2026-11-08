"""add allowed_formats to knowledge_bases

「切块策略即语料准入」：库里新增白名单列，并把策略列改成必填。

回填口径（存量行只有两种可能）：
- `structure` → `["markdown"]`：它的能力都建立在 Markdown 标题语法上；
- `size` → 五种全开：它不依赖任何结构。

策略列原本带 `server_default='structure'`，这次一并去掉——「没有策略」这件事
应当在插入时就失败，而不是被一个默认值悄悄掩盖。

Revision ID: cecd88875297
Revises: 4c7e8f9a0b1c
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "cecd88875297"
down_revision: Union[str, None] = "4c7e8f9a0b1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STRUCTURE_FORMATS = '["markdown"]'
_ALL_FORMATS = '["markdown", "text", "docx", "pdf", "html"]'


def upgrade() -> None:
    # 先加可空列 → 回填 → 再设非空：一步到位加 NOT NULL 在存量行上过不去。
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "allowed_formats",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        f"UPDATE knowledge_bases SET allowed_formats = '{_STRUCTURE_FORMATS}'::jsonb "
        "WHERE split_strategy = 'structure'"
    )
    op.execute(
        f"UPDATE knowledge_bases SET allowed_formats = '{_ALL_FORMATS}'::jsonb "
        "WHERE split_strategy = 'size'"
    )
    # 兜底：策略列有 CHECK 约束，正常不会有第三种值；万一被手工绕过也不留空值。
    op.execute(
        f"UPDATE knowledge_bases SET allowed_formats = '{_STRUCTURE_FORMATS}'::jsonb "
        "WHERE allowed_formats IS NULL"
    )
    op.alter_column("knowledge_bases", "allowed_formats", nullable=False)
    op.alter_column("knowledge_bases", "split_strategy", server_default=None)


def downgrade() -> None:
    op.alter_column("knowledge_bases", "split_strategy", server_default="structure")
    op.drop_column("knowledge_bases", "allowed_formats")
