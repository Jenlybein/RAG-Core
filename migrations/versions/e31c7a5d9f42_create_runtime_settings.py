"""create runtime_settings

运行时配置的覆盖层（票 #93）：``.env`` 是缺省，这张表只存被显式覆盖的行。

**不灌数据**。一行都没有时，进程读到的覆盖集合是空集，于是合并结果等于 `.env` 的值、
行为与改造前逐字一致——这是本票写进验收里的一条红线。反过来，若迁移里顺手把当前
``.env`` 灌成库里的行，第一次部署就会把「启动读环境变量」变成「启动读库」，而运维
以后改 `.env` 会**看起来没效果**（库里的行优先），排障成本极高。

**不加外键**：``updated_by`` 存账号 id，但账号可以被删；审计要能在被审计的行消失之后
仍然读得懂，配置表也一样。引用完整性由应用层校验，与既有四张业务表的风格一致。

**``value`` 用 JSONB**：覆盖值的类型横跨 bool / int / float / str，整列存 JSON 才不必
在读写两处各写一遍类型解析（``"false"`` 被当成 ``True`` 正是这类面板最典型的静默错误）。

Revision ID: e31c7a5d9f42
Revises: d4b7e2c5a890
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e31c7a5d9f42"
down_revision: Union[str, None] = "d4b7e2c5a890"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        # 主键就是键名本身：「一个键最多一行」是这张表的全部约束。
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
