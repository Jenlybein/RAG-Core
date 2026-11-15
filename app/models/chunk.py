"""表 3：chunks —— 检索的最小返回单元。

embedding 是硬约束 Vector(1536)：维度改了就要改列 + 迁移 + 全量重建索引。
字段名 meta 映射到数据库列 metadata（metadata 是 SQLAlchemy 的保留名）。

content 是**切片原文**（原样返回给调用方）；送进 embedding 的检索文本不落库，
只在索引期由 ``app.utils.retrieval_text`` 算出来。

``index_version`` 是数据范围的一部分（ADR-0010 §5 / ADR-0025）：它与 ``kb_id`` 组成
**配对映射**（``kb_id → version``），检索永远 pin 在各库的**生效版本**上，构建期间照旧
查旧的 active。版本由服务端从库行解析（:func:`app.models.index_version.resolve_index_versions`），
**不是请求参数**。复合索引 ``(tenant_id, kb_id, index_version)`` 供范围检索使用。

**范围条件只有一份**（:func:`scope_filter`）：向量召回与「空结果」计数都编译同一条
配对条件。写两遍的下场是其中一遍迟早退化成两个独立 ``IN``——那会匹配出「A 库配 B 库
版本」这种根本不存在的组合，而且不报错。
"""

from collections.abc import Mapping

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, Integer, String, Text, and_, false, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from app.db.base import Base

# 切片默认索引版本；列默认值、索引期写入与迁移回填共用这一个常量。
DEFAULT_INDEX_VERSION = "v1"

# 切片默认稿号（第 1 稿）。列默认值、索引期写入与关键词文档共用这一个常量：
# 与 ``DEFAULT_INDEX_VERSION`` 同一条理由——两处各写一个字面量，迟早一边改了另一边没改。
DEFAULT_DOC_VERSION = 1

#: 路标切片的类型名：除了一句「参见别处」之外没有实质内容的片。
#:
#: **它现在只剩「消费端」**：轻量图谱（ADR-0027）撤销之后，索引链路不再把哪一片标成
#: ``reference_pointer``（标记它的那条规则是图谱抽取的一部分，那段代码留在 ADR-0029
#: 记录的历史 tag 里）。取回过滤仍然保留——**旧版本的库里已经标好的
#: 那些片**还得继续被排除在召回之外，否则一次撤销会把「参见别处」当成命中重新放回来。
#: 删掉过滤等于对存量数据做一次静默的行为变更，而那正是这次收窄最不该发生的事。
CHUNK_TYPE_REFERENCE_POINTER = "reference_pointer"


def scope_filter(index_versions: Mapping[str, str]) -> ColumnElement[bool]:
    """把「kb_id → 索引版本」编译成**配对条件**：每对内部 AND，对与对之间 OR。

    为什么不写成两个独立的 ``IN``：``kb_id IN ('kb-a','kb-b')`` 配
    ``index_version IN ('v1','v2')`` 会匹配出「kb-a 的 v2」这种根本不存在的组合。
    单版本时两种写法完全等价，多版本并存时就分叉——那是越权或脏数据，而且不会
    报错。配对关系只能从映射里读出来，不能从两个平行数组拼（与关键词侧的
    ``build_bm25_query`` 同一条规矩）。

    空映射返回**恒假**而不是恒真：拿不到范围却不过滤，等于把整个租户的库都召回，
    比空结果严重得多。

    调用它之前，映射必须已经由 ``resolve_index_versions`` 解析过——那个函数会拒绝
    「有库没有生效版本」的情况，于是这里不会收到一个值为 ``None`` 的配对。
    """

    pairs = [
        and_(Chunk.kb_id == kb_id, Chunk.index_version == index_version)
        for kb_id, index_version in index_versions.items()
    ]
    if not pairs:
        return false()
    return or_(*pairs)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_tenant_id_kb_id_index_version", "tenant_id", "kb_id", "index_version"),
        Index("ix_chunks_document_id_doc_version", "document_id", "doc_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    index_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_INDEX_VERSION,
        server_default=DEFAULT_INDEX_VERSION,
    )
    # 稿号：这一片属于第几稿（与「索引版本」正交，见 ADR-0005）。
    #
    # 它是**真实列**而不是 JSONB 里的一个键（#172）：稿号只有一个来源。写入侧怎么填
    # 属于后续的票；这里只保证列在、非空、默认第 1 稿——存量切片在迁移里按所属文档的
    # 当前稿指针回填，于是「切片稿号 = 文档指针」在迁移完成那一刻就成立。
    doc_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
