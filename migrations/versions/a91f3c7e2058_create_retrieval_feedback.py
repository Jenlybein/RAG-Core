"""create retrieval_feedback

反馈表（票 #105）：调用方对某一次检索的 1~5 分。

**为什么单独一张表，而不是往 ``retrieval_traces`` 加列**：归属索引是**只写不改的账本**
（一次检索一行，写完就不再变）；反馈天生要被覆盖（同一个 ``log_id`` 重打一次分，后写
覆盖）。把可更新的列塞进不改的账本里，那张表「写下的就是历史」这句话立刻不成立。
详见 ``app/models/retrieval_feedback.py`` 的模块文档与 ADR-0021 决策 3。

**不设外键**：归属索引是旁路账本，删行不该连带删掉反馈——审计要能在被引用的行消失之后
仍然读得懂。引用完整性由应用层校验（存在 / ``trace_id`` 对得上 / 属于当前租户，三种失败
对外同一个码 ``20021``，不泄漏「存在但不属于你」）。

**不灌数据**：迁移只建表。反馈是调用方写下的东西，没有任何一行该由迁移自己造出来。

Revision ID: a91f3c7e2058
Revises: 0e7a1c4b8d92
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a91f3c7e2058"
down_revision: Union[str, None] = "0e7a1c4b8d92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "retrieval_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        # 唯一索引把「一次检索只有一份反馈」钉进库里：并发首提交撞上它会失败成 20020，
        # 调用方重试即覆盖，而不是留下两条互相矛盾的行。
        sa.Column("log_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        # 可空：调用方只想打分不想解释时它就是 NULL，不是空串。
        sa.Column("comment", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retrieval_feedback_tenant_id", "retrieval_feedback", ["tenant_id"])
    op.create_index(
        "ix_retrieval_feedback_log_id", "retrieval_feedback", ["log_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_feedback_log_id", table_name="retrieval_feedback")
    op.drop_index("ix_retrieval_feedback_tenant_id", table_name="retrieval_feedback")
    op.drop_table("retrieval_feedback")
