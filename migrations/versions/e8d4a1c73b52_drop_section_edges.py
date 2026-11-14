"""drop section_edges（撤销轻量图谱：ADR-0027 已被 ADR-0029 取代）

第 11 步的轻量图谱（ADR-0027 / #152）在 #138 的范围变更里被撤销。建它的那条迁移
``b3d7e1a95c24`` 一个字都不改（它已经在真库上跑过，``alembic_version`` 认的是它的
revision id），撤销靠**新增这一条** drop。

## upgrade：连索引一起删

三条索引都是这张表自己的（``ix_section_edges_tenant_id`` / ``_scope_source`` /
``_scope_target``），随表一起删；约束（唯一约束 + 两条 CHECK）随表定义一起消失，不必
单独 drop。删表之前不搬数据：边只是**候选池的补充材料**，没有任何一条检索结果、引用块
或归属索引行依赖它——重建一遍索引就能重新得到「没有边」的这个库，而数据本身也不再被
任何代码读取（关系边模型与仓储在同一票里删掉了）。

## downgrade：只重建结构，**不恢复数据**

回滚把表按原样建回来（十一列、三列作用域、两端四列、边型、证据、创建时间，加三条约束
与三条索引），但库里那些边**不会回来**——它们在 ``upgrade`` 里已经被删掉了，而本迁移
没有、也不可能保留一份边数据的副本。要恢复数据只有一条路：checkout 到撤销之前的代码
（tag ``step11-with-graph-and-orchestration``）重新建边。

这一点写在这里而不是只写在 PR 说明里：**``downgrade`` 不是时间机器**，它把 schema 复原，
把「这条迁移做过什么」留在历史的可回滚范围内；数据层的损失是这次能力撤销的既定代价
（ADR-0029 的翻案条件里也写着「要重做图谱得先重新测量」）。

Revision ID: e8d4a1c73b52
Revises: b3d7e1a95c24（撤销它建出来的那张关系边表）
Create Date: 2026-11-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8d4a1c73b52"
down_revision: Union[str, None] = "b3d7e1a95c24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 八列唯一约束（与 ``b3d7e1a95c24`` 逐字一致：回滚要把结构复原，不是「差不多能跑」）。
_UNIQUE_COLUMNS = (
    "tenant_id",
    "kb_id",
    "index_version",
    "source_document_id",
    "source_section_id",
    "target_document_id",
    "target_section_id",
    "relation_type",
)


def upgrade() -> None:
    op.drop_index("ix_section_edges_scope_target", table_name="section_edges")
    op.drop_index("ix_section_edges_scope_source", table_name="section_edges")
    op.drop_index("ix_section_edges_tenant_id", table_name="section_edges")
    op.drop_table("section_edges")


def downgrade() -> None:
    # 只重建结构：表里曾经有哪些边不会回来（见模块文档「downgrade」）。
    op.create_table(
        "section_edges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=36), nullable=False),
        sa.Column("index_version", sa.String(length=32), nullable=False),
        sa.Column("source_document_id", sa.String(length=36), nullable=False),
        sa.Column("source_section_id", sa.String(length=64), nullable=False),
        sa.Column("target_document_id", sa.String(length=36), nullable=False),
        sa.Column("target_section_id", sa.String(length=64), nullable=False),
        sa.Column("relation_type", sa.String(length=16), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(*_UNIQUE_COLUMNS, name="uq_section_edges_identity"),
        sa.CheckConstraint(
            "relation_type IN ('reference', 'sibling', 'parent')",
            name="ck_section_edges_relation_type",
        ),
        sa.CheckConstraint(
            "source_document_id <> target_document_id"
            " OR source_section_id <> target_section_id",
            name="ck_section_edges_no_self_loop",
        ),
    )
    op.create_index("ix_section_edges_tenant_id", "section_edges", ["tenant_id"])
    op.create_index(
        "ix_section_edges_scope_source",
        "section_edges",
        ["tenant_id", "kb_id", "index_version", "source_document_id", "source_section_id"],
    )
    op.create_index(
        "ix_section_edges_scope_target",
        "section_edges",
        ["tenant_id", "kb_id", "index_version", "target_document_id", "target_section_id"],
    )
