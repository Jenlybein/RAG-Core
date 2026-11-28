"""表 3：chunks —— 检索的最小返回单元。

embedding 是硬约束 Vector(1536)：维度改了就要改列 + 迁移 + 全量重建索引。
字段名 meta 映射到数据库列 metadata（metadata 是 SQLAlchemy 的保留名）。

content 是**切片原文**（原样返回给调用方）；送进 embedding 的检索文本不落库，
只在索引期由 ``app.utils.retrieval_text`` 算出来。

``generation`` 是**可见性**的一部分（现场九·第四步 / ADR-0025 第 2 次修订）：它记这一片
属于该篇的第几代产物，与 ``documents.current_index_generation`` 一比就决定「这一片算不算
现在服务的那一份」。改造前这件事由库级的 ``index_version = 生效版本`` 表达——代下放到篇级
之后那条相等条件不再等价（构建期间新代写在还没上线的版本里）。

**可见性条件只有一份**（:func:`current_generation_filter` + :func:`current_draft_filter`）：
向量召回、界面片数、折叠取整节、补父节、空结果判定都编译同一条。写两遍的下场是其中一遍
迟早漏掉一条轴——那会让旧代或旧稿的切片混进结果，而且不报错。
"""


from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from app.db.base import Base
from app.models.document import Document

# 切片默认索引版本；列默认值、索引期写入与迁移回填共用这一个常量。
DEFAULT_INDEX_VERSION = "v1"

#: 切片默认稿号（第 1 稿）：列默认值、关键词文档缺省值与迁移回填共用这一个常量。
DEFAULT_DOC_VERSION = 1

#: 切片「属于第几代索引产物」的缺省值。与迁移回填的第 1 代同源（现场九）。
DEFAULT_GENERATION = 1

#: 路标切片的类型名：除了一句「参见别处」之外没有实质内容的片。
#:
#: **它现在只剩「消费端」**：轻量图谱（ADR-0027）撤销之后，索引链路不再把哪一片标成
#: ``reference_pointer``（标记它的那条规则是图谱抽取的一部分，那段代码留在 ADR-0029
#: 记录的历史 tag 里）。取回过滤仍然保留——**旧版本的库里已经标好的
#: 那些片**还得继续被排除在召回之外，否则一次撤销会把「参见别处」当成命中重新放回来。
#: 删掉过滤等于对存量数据做一次静默的行为变更，而那正是这次收窄最不该发生的事。
CHUNK_TYPE_REFERENCE_POINTER = "reference_pointer"


def current_draft_filter() -> ColumnElement[bool]:
    """「这一片属于当前稿」：``chunks.doc_version = documents.current_version``（#174）。

    这是全仓**唯一**一处「当前稿」谓词：向量召回、折叠取整节、空结果计数与溯源回查
    都编译它，写四遍的下场是其中一遍迟早漏掉——而漏掉的那一处不会报错，只会「多召回
    一份旧稿」或「把旧稿的切片当成当前稿的」。

    为什么是**列与列比较**而不是应用层传一个稿号进来：

    * 指针在文档行上（``documents.current_version``），由库来算就不会出现「缓存里的
      指针过期」这种只在切稿瞬间成立的故障；
    * 调用方因此不需要先查一次文档表——少一次往返，也少一个能传错的值。

    前置条件：语句里必须 JOIN 了 ``documents``（``Document``），否则条件编译不出来。
    不用 ``JOIN`` 的写法（先把当前稿号读出来再传参）会把「读指针」与「按指针过滤」
    拆成两个时刻，中间正好是别人翻指针的窗口（ADR-0005）。

    与 :func:`current_generation_filter` 正交：那个管「哪一次重建」（代），这个管「哪一稿」。
    """

    return Chunk.doc_version == Document.current_version


def current_generation_filter() -> ColumnElement[bool]:
    """「这一片属于该文档的**当前代**产物」：``chunks.generation = documents.current_index_generation``。

    与 :func:`current_draft_filter` 是同一种写法（列与列比较、由库求值、语句必须 JOIN
    ``documents``），因为它们是两根**正交**轴的同一套纪律：

    * 稿次那一条回答「这一片装的是不是现在生效的那份正文」；
    * 这一条回答「这一片是不是现在生效的那份产物」。

    改造之前「哪份产物生效」是**库级**的（``index_version = 生效版本``），所以一条相等条件
    就够了。代下放到篇级之后，每篇各有自己的当前代，库级的相等条件**不再等价于**它——少了
    这一条，留在库里的旧代切片会被当成现行内容一起召回（症状是同一段内容回答两次，或者引用
    了上一版已经删掉的条款），而它不报错。

    为什么不把当前代读出来当参数传：指针在文档行上，由库来比就不会出现「缓存里的代过期」，
    调用方也不必先查一次文档表（多一次往返、多一个能传错的值）。

    ## 为什么它必须**替代**版本条件，而不是乘在旁边

    单独把这一条乘到「``index_version = 生效版本``」旁边是**错的**，症状很具体：起一次构建
    往新版本 v5 写时，写路径（``advance_index_generation``）会把该篇指针推到第 2 代，而
    v5 还没上线、线上仍是 v4——于是这篇在 v4 里「当前代 = 2」的切片一片也没有，检索与计数
    双双归零（真库实测过：``count_in_kb`` = 0）。也就是说它会**破坏「构建期间照旧服务旧
    内容」**这条已经成立的性质。

    所以第四步是把版本条件**换掉**（ADR-0025 第 2 次修订）：产物只按代看，库级版本轴退居
    构建真值。

    ## 关键词那一路为什么不需要它

    ES 无法 JOIN 文档表，但它也不需要：索引里每篇**只有一份**（写入总是「先按文档全清、
    再写新的一份」），所以「查到的就是当前代」。两个后端、两种实现、同一个语义。
    """

    return Chunk.generation == Document.current_index_generation


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_tenant_id_kb_id_index_version", "tenant_id", "kb_id", "index_version"),
        Index("ix_chunks_document_id_doc_version", "document_id", "doc_version"),
        # 按「租户 + 库 + 代」收窄：检索与「保留最近 N 代」的回收都走它。
        Index("ix_chunks_tenant_id_kb_id_generation", "tenant_id", "kb_id", "generation"),
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
    # 它是**真实列**而不是 JSONB 里的一个键（#172）：稿号只有一个来源。检索侧自
    # #174 起按它与文档的当前稿指针对齐（:func:`current_draft_filter`），所以这一列
    # 的默认值必须与写入缺省、关键词文档缺省、迁移回填同源（``DEFAULT_DOC_VERSION``）。
    doc_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_DOC_VERSION,
        server_default=str(DEFAULT_DOC_VERSION),
    )
    # 这一片属于该文档的第几代**索引产物**（第 17 步现场九）。
    #
    # 与 ``doc_version`` 正交：那个管「这一片装的是哪一稿正文」，这个管「这一片是哪一次
    # 索引跑出来的」。改造前它由库级的 ``index_version`` 承担，而库级判据要与篇级稿次对齐
    # 时就只能靠「统一稿号」硬凑——那正是本步要拆掉的东西。
    #
    # 检索侧按它和 ``documents.current_index_generation`` 做列比较（与
    # :func:`current_draft_filter` 同一套写法，不传参、不在应用层缓存）。
    generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_GENERATION,
        server_default=str(DEFAULT_GENERATION),
    )
