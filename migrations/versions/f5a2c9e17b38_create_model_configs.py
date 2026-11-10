"""create model_configs

多模型配置表（票 #94）：一行 = 一套可用的模型接入。角色指向**不在这张表里**，它是
``runtime_settings`` 的两条指针键（``model.role.fast`` / ``model.role.strong``），
见 ``app/models/model_config.py`` 的模块文档。

**不灌数据**。一行都没有时，角色指针也是空的，于是 Provider 装配回落到 ``.env`` 的
单供应商——离线与 CI 的行为与改造前逐字一致（本票的回归红线）。反过来，若迁移里顺手
把当前 ``.env`` 灌成库里的行，第一次部署就会把「启动读环境变量」变成「读库里的指向」，
运维以后改 ``.env`` 会**看起来没效果**，排障成本极高（与 #93 那条同样的理由）。

**不加外键**：「被角色引用就不许删」这条判定要先把两个指针键读出来比对，外键表达不了
「两个键里的任意一个」；引用完整性因此由应用层校验，与既有四张业务表同一套风格。

Revision ID: f5a2c9e17b38
Revises: e31c7a5d9f42
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a2c9e17b38"
down_revision: Union[str, None] = "e31c7a5d9f42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_configs",
        # 裸字符串 id：与既有业务表一致，引用方（角色指针）只存这个字符串。
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        # 空串表示「用该供应商的默认地址」，所以有默认值而不是可空。
        sa.Column("base_url", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=128), nullable=False),
        # 明文凭证（ADR-0004），缺省空串 = 这一条还没填 Key（装配时退回空实现）。
        sa.Column("api_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("model_configs")
