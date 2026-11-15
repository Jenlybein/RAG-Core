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
    DEFAULT_INDEX_VERSION,
    Chunk,
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
    ) -> None:
        """删该文档的切片行；``index_version`` 非空时只删那一版（#142）。

        **带版本删是为了不弄空线上那一版**：重建 v2 时若把这份文档的 v1 行也删了，
        构建期间的检索会在一段时间里查不到它——而「构建期间线上照旧查旧版本」恰恰要求
        旧行原样不动。
        """

        statement = delete(Chunk).where(
            Chunk.document_id == document_id, Chunk.tenant_id == tenant_id
        )
        if index_version is not None:
            statement = statement.where(Chunk.index_version == index_version)
        await db.execute(statement)
        await db.flush()