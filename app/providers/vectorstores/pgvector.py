"""pgvector 实现：用 SQLAlchemy 直接写 chunks 表。

score = 1 - 余弦距离，在 Python 侧换算成「越大越相关」；SQL 只按距离升序排。
写入只 flush，不 commit——事务由调用它的 Service 统一提交。

**读侧是异步的**（第 11 步票 #127）：``similarity_search*`` 收的是 ``AsyncSession``，
``await db.execute(...)`` 之后那条同步 psycopg 查询不再存在——检索链路上因此没有任何
「会卡住事件循环的同步调用」，第 10 步的执行池（``app/core/loop_pool.py``）也就不需要了。

**四个方法的会话类型都是 ``AsyncSession``**（第 11 步票 #127 / #130 / #132）：索引链路、
数据面删除与控制面删除都已经是异步栈，迁移期那个联合类型已经收窄成这一种。
"""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.chunk import (
    CHUNK_TYPE_REFERENCE_POINTER,
    DEFAULT_DOC_VERSION,
    DEFAULT_GENERATION,
    DEFAULT_INDEX_VERSION,
    Chunk,
    current_draft_filter,
    current_generation_filter,
    scope_filter,
)
from app.models.document import Document
from app.providers.vectorstores.base import VectorStore


def not_reference_pointer() -> ColumnElement[bool]:
    """召回侧的「排除路标切片」条件（#152，ADR-0027 决策 3）。

    路标切片（除了一句「详见《X》2.4 节」之外没有实质内容）**只用于建边，不参与召回**：
    它没有信息量，进候选池只是占名额，还会把「命中一句『参见别处』」变成一次成功的召回。

    **为什么是 ``coalesce(...) != 'reference_pointer'`` 而不是直接比较**：``metadata`` 里
    没有 ``chunk_type`` 键的老切片，``->>`` 出来是 ``NULL``，而 ``NULL != 'x'`` 在 SQL 里
    是 ``NULL``——整行会被静默排除。没有这个键的切片恰恰**不是**路标，必须留在召回里。
    """

    chunk_type = func.coalesce(Chunk.meta["chunk_type"].astext, "")
    return chunk_type != CHUNK_TYPE_REFERENCE_POINTER


def build_scope_filter(index_versions: dict[str, str]) -> ColumnElement[bool]:
    """把「kb_id → 索引版本」编译成**配对条件**（实现见 :func:`app.models.chunk.scope_filter`）。

    为什么必须配对、为什么不能写成两个独立的 ``IN``：见 :func:`app.models.chunk.scope_filter`
    的说明。这里只是它的转发，保留本模块的公开名字——范围条件的**唯一实现**在模型层，
    因为「空结果」计数（``chunk_repository.count_by_kb_ids``）也要编译同一条条件，
    而仓储层不该 import 一个 Provider。
    """

    return scope_filter(index_versions)


class PgVectorStore(VectorStore):
    async def add_chunks(self, db: AsyncSession, chunks: list[dict]) -> None:
        db.add_all(
            [
                Chunk(
                    id=c["id"],
                    tenant_id=c["tenant_id"],
                    kb_id=c["kb_id"],
                    document_id=c["document_id"],
                    content=c["content"],
                    embedding=c["embedding"],
                    meta=c.get("meta", {}),
                    # 索引版本随行落库；调用方没给时用列默认值，不写 NULL。
                    index_version=c.get("index_version", DEFAULT_INDEX_VERSION),
                    # 稿号是**真实列**（#172 的迁移加的），写入侧在这一票跟上（#173）：
                    # 调用方把「这一片属于第几稿」放在切片字典的顶层。缺省值与列默认值
                    # 同源（第 1 稿），绝不写 NULL——列是非空的。
                    doc_version=c.get("doc_version") or DEFAULT_DOC_VERSION,
                    # 索引代（现场九）：调用方把「这一片属于第几代产物」放在切片字典顶层。
                    # 缺省值与列默认值同源，绝不写 NULL——列是非空的。
                    generation=c.get("generation") or DEFAULT_GENERATION,
                )
                for c in chunks
            ]
        )
        await db.flush()

    async def similarity_search(
        self,
        db: AsyncSession,
        query_vector: list[float],
        *,
        tenant_id: str,
        kb_id: str,
        top_k: int,
    ) -> list[dict]:
        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(
                Chunk.id.label("chunk_id"),
                Chunk.document_id,
                Chunk.content,
                Document.title,
                distance,
            )
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.tenant_id == tenant_id,
                Chunk.kb_id == kb_id,
                not_reference_pointer(),
                current_draft_filter(),
            )
            .order_by(distance)
            .limit(top_k)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "chunk_id": r.chunk_id,
                "document_id": r.document_id,
                "title": r.title,
                "content": r.content,
                "score": 1 - float(r.distance),
            }
            for r in rows
        ]

    async def similarity_search_scope(
        self,
        db: AsyncSession,
        query_vector: list[float],
        *,
        tenant_id: str,
        index_versions: dict[str, str],
        top_k: int,
    ) -> list[dict]:
        """范围版召回：一条 SQL 覆盖「kb_id → 索引版本」里的全部配对。

        与 :meth:`similarity_search` 的差别只有两处：库/版本过滤换成配对条件，
        以及每行**自带 ``kb_id``**（一次跨库召回，结果必须能归属到某个库）。
        打分口径完全一致：``score = 1 - 余弦距离``。

        两条路径都带「只看当前稿」（``current_draft_filter``，#174）：两稿切片并存的
        时刻（切稿之后、保留策略删掉旧稿之前）漏掉这一条，旧稿切片会以同等分数挤进
        TopK，而结果看起来完全正常——只是同一段内容出现了两份。

        排序只按距离升序、一次取 ``top_k``：这是「一次全局召回」。分库各取
        ``top_k`` 再合并看着公平，实际让不相关的库也各占名额。
        """

        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(
                Chunk.id.label("chunk_id"),
                Chunk.kb_id,
                Chunk.document_id,
                Chunk.content,
                Document.title,
                distance,
            )
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.tenant_id == tenant_id,
                build_scope_filter(index_versions),
                not_reference_pointer(),
                current_draft_filter(),
                # 第三轴（现场九·第三步）：只召回该篇**当前代**的产物。旧代切片按保留策略
                # 留在库里（回滚要用），少了这一条它们会以同等分数挤进 TopK，而结果看起来
                # 完全正常——只是同一段内容出现两份。
                current_generation_filter(),
            )
            .order_by(distance)
            .limit(top_k)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "chunk_id": r.chunk_id,
                "kb_id": r.kb_id,
                "document_id": r.document_id,
                "title": r.title,
                "content": r.content,
                "score": 1 - float(r.distance),
            }
            for r in rows
        ]

    async def similarity_search_document_version(
        self,
        db: AsyncSession,
        query_vector: list[float],
        *,
        tenant_id: str,
        document_id: str,
        doc_version: int,
        index_version: str,
        top_k: int,
    ) -> list[dict]:
        """锁定某篇某稿的召回：按版本只读直查的底层（#176）。

        与 :meth:`similarity_search_scope` 的差别只有条件：范围从「``kb_id → 索引版本``
        的配对映射」换成「一篇文档 + 一个稿号 + 一个索引版本」，而**``current_draft_filter``
        被刻意去掉**——它的实现是 ``chunks.doc_version = documents.current_version``
        （列与列比较），历史稿的切片一片都对不上；直查历史稿却带着它，结果永远是空，
        而那看起来完全像「这一稿没内容」——正是这条路径要排查的那种误判。

        去掉它带来两个必须由参数兜住的约束：

        * ``tenant_id``：少了它就是跨租户召回（ADR-0006）；
        * ``index_version``：旧索引版本的行还留在库里（#142 的「构建期间查旧版本」），
          不按版本收窄会把两次构建的切片混进结果。

        **默认路径一个字没改**：不传稿号的召回仍走 :meth:`similarity_search_scope`，
        仍然只看当前稿（``tests/test_retrieval_current_draft.py`` 与
        ``tests/test_document_version_search.py`` 各钉一遍）。

        每行自带 ``doc_version``：回包要说清「这条是第几稿的片」，而稿号是真实列，
        读它比相信调用方传入的稿号更可靠。
        """

        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(
                Chunk.id.label("chunk_id"),
                Chunk.kb_id,
                Chunk.document_id,
                Chunk.doc_version,
                Chunk.content,
                Document.title,
                distance,
            )
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.tenant_id == tenant_id,
                Chunk.document_id == document_id,
                Chunk.doc_version == doc_version,
                Chunk.index_version == index_version,
                not_reference_pointer(),
            )
            .order_by(distance)
            .limit(top_k)
        )
        rows = (await db.execute(stmt)).all()
        return [
            {
                "chunk_id": r.chunk_id,
                "kb_id": r.kb_id,
                "document_id": r.document_id,
                "doc_version": r.doc_version,
                "title": r.title,
                "content": r.content,
                "score": 1 - float(r.distance),
            }
            for r in rows
        ]

    async def count_by_scope(
        self, db: AsyncSession, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        """数该库在某版本里的向量条数（``ready`` 前的四项计数之一）。

        在 pgvector 实现里向量就存在切片行上，所以这个数字与「切片数」天然相等——
        它仍然是**独立的一次读数**：换成外部向量库时两者会分叉，而「四项计数相等」这道
        闸门必须在那种实现上照旧成立（ADR-0025 决策一）。带租户、带库、带版本，一个都
        不能省：少了任何一个都会把别的范围的行数算进来，闸门于是静默失灵。
        """

        stmt = (
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.tenant_id == tenant_id,
                Chunk.kb_id == kb_id,
                Chunk.index_version == index_version,
            )
        )
        return int((await db.execute(stmt)).scalar_one())

    async def delete_by_document_id(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
        doc_version: int | None = None,
    ) -> None:
        """删该文档的切片行；两个范围参数都可缺省，缺省即「不按这一维收窄」。

        ``index_version`` 非空时只删那一版（#142）。

        **带版本删是为了不弄空线上那一版**：重建 v2 时若把这份文档的 v1 行也删了，
        构建期间的检索会在一段时间里查不到它——而「构建期间线上照旧查旧版本」恰恰要求
        旧行原样不动。

        ``doc_version`` 非空时只删那一稿（#173）：重建第 2 稿时第 1 稿的切片一个字节都
        不动（ADR-0005 D4 选 B「每稿各留切片」）。两者正交：**同一稿可能被多轮索引版本
        各写一遍**，所以两个条件要同时带。
        """

        statement = delete(Chunk).where(
            Chunk.document_id == document_id, Chunk.tenant_id == tenant_id
        )
        if index_version is not None:
            statement = statement.where(Chunk.index_version == index_version)
        if doc_version is not None:
            statement = statement.where(Chunk.doc_version == doc_version)
        await db.execute(statement)
        await db.flush()
