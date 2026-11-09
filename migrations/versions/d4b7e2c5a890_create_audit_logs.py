"""create audit_logs

控制面改动的账本：谁（``actor_id``，可空——系统动作没有人）、什么时候（``created_at``）、
把什么（``target``）改成了什么（``before_snapshot`` / ``after_snapshot``）。

``action`` 建普通索引：审计列表按动作名筛是最常见的读法。两份快照都可空，于是「新增」
只有 after、「删除」只有 before，两种动作共用这一张表。

不带外键：被审计的行被删掉之后，这条记录仍然要读得懂。

Revision ID: d4b7e2c5a890
Revises: c3f8a1d24b67
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4b7e2c5a890"
down_revision: Union[str, None] = "c3f8a1d24b67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column(
            "before_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "after_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_action"), "audit_logs", ["action"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_logs_action"), table_name="audit_logs")
    op.drop_table("audit_logs")
