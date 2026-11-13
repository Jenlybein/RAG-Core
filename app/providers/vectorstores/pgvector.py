"""pgvector 实现：用 SQLAlchemy 直接写 chunks 表。

score = 1 - 余弦距离，在 Python 侧换算成「越大越相关」；SQL 只按距离升序排。
写入只 flush，不 commit——事务由调用它的 Service 统一提交。

**读侧是异步的**（第 11 步票 #127）：``similarity_search*`` 收的是 ``AsyncSession``，
``await db.execute(...)`` 之后那条同步 psycopg 查询不再存在——检索链路上因此没有任何
「会卡住事件循环的同步调用」，第 10 步的执行池（``app/core/loop_pool.py``）也就不需要了。

**四个方法的会话类型都是 ``AsyncSession``**（第 11 步票 #127 / #130 / #132）：索引链路、
数据面删除与控制面删除都已经是异步栈，迁移期那个联合类型已经收窄成这一种。
"""

from sqlalchemy import and_, delete, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.chunk import DEFAULT_INDEX_VERSION, Chunk
from app.models.document import Document
from app.providers.vectorstores.base import VectorStore


def build_scope_filter(index_versions: dict[str, str]) -> ColumnElement[bool]:
    """把「kb_id → 索引版本」编译成**配对条件**：每对内部 AND，对与对之间 OR。

    为什么不写成两个独立的 ``IN``：``kb_id IN ('kb-a','kb-b')`` 配
    ``index_version IN ('v1','v2')`` 会匹配出「kb-a 的 v2」这种根本不存在的组合。
    单版本时两种写法完全等价，多版本并存时就分叉——那是越权或脏数据，而且不会
    报错。配对关系只能从映射里读出来，不能从两个平行数组拼（与关键词侧的
    ``build_bm25_query`` 同一条规矩）。

    空映射返回**恒假**而不是恒真：拿不到范围却不过滤，等于把整个租户的库都召回，
    比空结果严重得多。
    """

    pairs = [
        and_(Chunk.kb_id == kb_id, Chunk.index_version == index_version)
        for kb_id, index_version in index_versions.items()
    ]
    if not pairs:
        return false()
    return or_(*pairs)


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
            .where(Chunk.tenant_id == tenant_id, Chunk.kb_id == kb_id)
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
            .where(Chunk.tenant_id == tenant_id, build_scope_filter(index_versions))
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

    async def delete_by_document_id(
        self, db: AsyncSession, document_id: str, *, tenant_id: str
    ) -> None:
        await db.execute(
            delete(Chunk).where(Chunk.document_id == document_id, Chunk.tenant_id == tenant_id)
        )
        await db.flush()