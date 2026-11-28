"""drop the library-level index version layer（库级索引版本整层退场）

第 17 步现场九的收尾：可见性判据已经下放到「篇级当前代 × 当前稿」
（``chunks.generation = documents.current_index_generation`` + 稿号对齐，ADR-0025 第 2 次
修订），库级索引版本这一层因此一律不再兼容，整层删除。

## upgrade：三刀，逐条 drop

1. **删表** ``index_build_versions``：整库构建的版本行状态机（building → ready → active →
   archived / failed）、真值收尾、四计数对账、激活、物理索引别名切换全部退场。表上的
   列（含后续迁移加的 ``params`` / ``converge_deadline`` / ``failed_documents`` /
   ``redispatch_count`` / ``auto_activate`` 等）随表一起消失，不必逐列 drop。
2. **删列** ``knowledge_bases.active_index_version``：它回答「这个库的哪一版产物在服务」。
   现在每篇各有自己的当前代，库这一级没有答案可给；检索入口那条「库还没有生效版本就
   拦下」的回填守卫也随它一起删除。
3. **删列** ``chunks.index_version`` 与只服务它的索引
   ``ix_chunks_tenant_id_kb_id_index_version``：它记录「这一片属于哪个库级版本」。有了
   ``chunks.generation``（这一片属于该篇的第几代产物）之后两者并存只会让人以为版本还在
   判可见性——而可见性里根本没有它。

## 为什么 ``documents.build_state`` 不在 drop 清单里

它**从来不是一个列**：「这一篇与某次构建的关系」（done / pending / failed）是控制面读模型
在内存里由构建行算出来的第二根轴（``console_kb_service._build_state_of``），随构建行一起
删除的是那段计算与那个响应字段，不是数据库结构。同一条理由适用于 ``documents`` 上任何
「与某次构建的关系」——本迁移核对过：那张表没有这类列。

## 停点：这条迁移**不可回退**

``downgrade`` 只把**结构和空表**建回来（三样东西按原样重建，含原索引与两条部分唯一索引），
**数据不会回来**：

* 版本行在 ``upgrade`` 里已经被删掉，没有副本；
* ``chunks.index_version`` 与 ``knowledge_bases.active_index_version`` 的历史值也没有副本。

回滚之后库里是「列在、值全是默认 / NULL」的状态，看起来像「所有切片都属于 v1、所有库都没
有生效版本」——那是**假数据**，比直接报错更糟。要真正回到旧模型只有一条路：checkout 到删列
之前的代码重新跑一遍构建。这一点写在这里而不是只写在 PR 说明里，与 ``e8d4a1c73b52`` 同一条
纪律：``downgrade`` 不是时间机器。

## 离线渲染

这条迁移是**纯 DDL**（没有回填、没有核对、不看库里的数据），所以不加
``_reject_offline_render``：``alembic upgrade --sql`` 能照常渲染出完整的删表 / 删列语句。

Revision ID: e8b2c47d91f5
Revises: b4e7c1a9d2f3（归属索引改记篇级代 + 缺代计数列，本步此前的唯一 head）
Create Date: 2026-11-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8b2c47d91f5"
down_revision: Union[str, None] = "b4e7c1a9d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 只服务 ``chunks.index_version`` 的复合索引（``6dfebeded96c`` 建的）。
_CHUNK_VERSION_INDEX = "ix_chunks_tenant_id_kb_id_index_version"


def upgrade() -> None:
    # 先删依赖列的索引，再删列；表在最后删（它不引用别的表，也没有别的东西引用它）。
    op.drop_index(_CHUNK_VERSION_INDEX, table_name="chunks")
    op.drop_column("chunks", "index_version")
    op.drop_column("knowledge_bases", "active_index_version")
    op.drop_table("index_build_versions")


def downgrade() -> None:
    # 只重建结构：见模块文档「停点」——数据不会回来，不要在生产上随手跑。
    op.create_table(
        "index_build_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("documents_planned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("doc_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("documents_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "converge_deadline", sa.DateTime(), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("failed_documents", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("redispatch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_activate", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "kb_id", "version", name="uq_index_build_versions_tenant_kb_version"
        ),
        sa.CheckConstraint(
            "status IN ('building', 'ready', 'active', 'archived', 'failed')",
            name="ck_index_build_versions_status",
        ),
    )
    op.create_index(
        op.f("ix_index_build_versions_tenant_id"),
        "index_build_versions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_index_build_versions_kb_id"), "index_build_versions", ["kb_id"], unique=False
    )
    op.create_index(
        "uq_index_build_versions_active",
        "index_build_versions",
        ["tenant_id", "kb_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_index_build_versions_building",
        "index_build_versions",
        ["tenant_id", "kb_id"],
        unique=True,
        postgresql_where=sa.text("status = 'building'"),
    )

    # 两列按原迁移的形状建回（``6dfebeded96c`` / ``e41b7c9d2a63``）：列在、但没有值，
    # 空值本身是「这条迁移删过什么」的诚实状态，而不是「版本又回来了」。
    op.add_column(
        "chunks",
        sa.Column("index_version", sa.String(length=32), nullable=False, server_default="v1"),
    )
    op.create_index(_CHUNK_VERSION_INDEX, "chunks", ["tenant_id", "kb_id", "index_version"])
    op.add_column(
        "knowledge_bases",
        sa.Column("active_index_version", sa.String(length=32), nullable=True),
    )
