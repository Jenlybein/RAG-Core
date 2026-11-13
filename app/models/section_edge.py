"""表 7：section_edges —— 节与节之间的**有向关系边**（ADR-0027，第 11 步票 #152）。

一行 = 一条有向边：作用域（租户、库、版本）+ 两端（文档 id + 节 id）+ 关系类型。

**为什么两端都带文档 id**：两份文档都叫「退款 > 流程」时，光有 ``section_id`` 无法区分
——节 id 本身是 ``stable_hash(文档 id : 标题路径)``，已经含文档 id，但把它拆成两列是
故意的：排障时要能直接读「边从哪份文档指到哪份文档」，而不是拿两个 64 位哈希去反推。
（ADR-0025 决策五 / :mod:`app.utils.section_identity`。）

**为什么带版本**：边必须与切片在**同一个构建产物**里才配对。旧边指向新版本里同 id 的节，
不会报错，只会给出一个错答案——这正是 ADR-0025 决策一要挡的那种故障。

**为什么只有三种关系**：``reference`` / ``sibling`` / ``parent`` 覆盖文档里真实存在的结构
关系；语义相近不是结构关系，按相似度猜边会在回答里长成假引用（ADR-0027 决策 2、3）。

**同一条边不许重复插入**，由 :data:`UNIQUE_EDGE_COLUMNS` 上的唯一约束与写入侧的
``ON CONFLICT DO NOTHING`` 两层保证：抽取是「同一节里两片各命中一次同一条引用」这种
重复的天然来源，只靠代码去重的话，一次并发重建就会撞进去。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now

#: 三种边型（与 ADR-0027 决策 2 的表一一对应）。**不加第四种**：每加一种都要回答
#: 「它该不该参与注入、优先级排第几、抽取规则怎么验」。
RELATION_REFERENCE = "reference"
RELATION_SIBLING = "sibling"
RELATION_PARENT = "parent"
RELATION_TYPES: tuple[str, ...] = (RELATION_REFERENCE, RELATION_SIBLING, RELATION_PARENT)

#: 唯一约束的列：作用域 + 两端 + 边型。同一条边再插一次是幂等的，不是「两条边」。
UNIQUE_EDGE_COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "kb_id",
    "index_version",
    "source_document_id",
    "source_section_id",
    "target_document_id",
    "target_section_id",
    "relation_type",
)


class SectionEdge(Base):
    __tablename__ = "section_edges"
    __table_args__ = (
        UniqueConstraint(*UNIQUE_EDGE_COLUMNS, name="uq_section_edges_identity"),
        CheckConstraint(
            "relation_type IN ('reference', 'sibling', 'parent')",
            name="ck_section_edges_relation_type",
        ),
        # 自环边没有意义（一节引用自己）：抽取侧本来就不产，约束是第二道防线。
        CheckConstraint(
            "source_document_id <> target_document_id"
            " OR source_section_id <> target_section_id",
            name="ck_section_edges_no_self_loop",
        ),
        # 「这一节往外指了谁」与「谁指向了这一节」是两个查询方向，各一个索引。
        Index(
            "ix_section_edges_scope_source",
            "tenant_id",
            "kb_id",
            "index_version",
            "source_document_id",
            "source_section_id",
        ),
        Index(
            "ix_section_edges_scope_target",
            "tenant_id",
            "kb_id",
            "index_version",
            "target_document_id",
            "target_section_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kb_id: Mapped[str] = mapped_column(String(36))
    #: 版本名，与 ``chunks.index_version`` 同宽同值域（``v1`` / ``v2``…）。
    index_version: Mapped[str] = mapped_column(String(32))
    #: 边的起点：出边的那个节。
    source_document_id: Mapped[str] = mapped_column(String(36))
    source_section_id: Mapped[str] = mapped_column(String(64))
    #: 边的终点：被指向的那个节。
    target_document_id: Mapped[str] = mapped_column(String(36))
    target_section_id: Mapped[str] = mapped_column(String(64))
    #: 三种边型之一（见 :data:`RELATION_TYPES`），由 CHECK 约束兜住。
    relation_type: Mapped[str] = mapped_column(String(16))
    #: 抽取出这条边的证据：命中的原文片段、章节号、匹配到的显式表达。排障时先看这里。
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = [
    "RELATION_PARENT",
    "RELATION_REFERENCE",
    "RELATION_SIBLING",
    "RELATION_TYPES",
    "UNIQUE_EDGE_COLUMNS",
    "SectionEdge",
]
