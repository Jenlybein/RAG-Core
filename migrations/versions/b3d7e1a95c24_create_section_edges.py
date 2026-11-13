"""create section_edges（关系表：作用域 + 两端 + 边型 + 证据，ADR-0027）

第 11 步票 #152 的落法：**先有表，再有抽取器**。表结构与 ADR-0027 决策 1 逐项对应：

| 列 | 它回答什么 |
| --- | --- |
| ``tenant_id`` / ``kb_id`` / ``index_version`` | 这条边属于哪一次构建产物（ADR-0025） |
| ``source_document_id`` + ``source_section_id`` | 边的起点是哪份文档的哪一节 |
| ``target_document_id`` + ``target_section_id`` | 终点同理 |
| ``relation_type`` | ``reference`` / ``sibling`` / ``parent``，CHECK 约束限定为这三个值 |
| ``metadata`` | 抽取证据（命中原文片段与章节号），排障用，不参与检索 |

三条约束**写进库**而不是写在注释里：

1. ``uq_section_edges_identity``：作用域 + 两端 + 边型唯一——同一条边再插一次是幂等的；
2. ``ck_section_edges_relation_type``：第四种边型插不进来（ADR-0027「边型只有三种」）；
3. ``ck_section_edges_no_self_loop``：一节不能引用自己。

两张索引分别服务「谁从这一节出去」与「谁指向这一节」两个方向。跨库引用在**同一租户**
内是允许的（ADR-0027 决策 5），所以这里**没有**「目标库必须等于源库」的约束——越权边界
在查询侧按本次请求的库与版本过滤，不在建边侧删关系。

**不新增任何列到既有表**，也不回填：边是构建产物，历史版本里没有边就是「没有边」，不
需要给旧数据编一个出来（本迁移之前也没有任何抽取器产出过边）。

``downgrade`` 干净删表：回滚之后剩下的就是这条迁移之前的那张库。

Revision ID: b3d7e1a95c24
Revises: e41b7c9d2a63（#141 的索引构建版本；本票与 #150 并行，合并时接在当时的链尾）
Create Date: 2026-11-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b3d7e1a95c24"
down_revision: Union[str, None] = "e41b7c9d2a63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 本票迁移的八列唯一约束（列名与 ``app.models.section_edge.UNIQUE_EDGE_COLUMNS`` 逐字一致）。
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


def downgrade() -> None:
    op.drop_index("ix_section_edges_scope_target", table_name="section_edges")
    op.drop_index("ix_section_edges_scope_source", table_name="section_edges")
    op.drop_index("ix_section_edges_tenant_id", table_name="section_edges")
    op.drop_table("section_edges")
