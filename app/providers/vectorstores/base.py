"""VectorStore 抽象。

判据：向量写入与相似度检索归它，普通 CRUD 归 ChunkRepository。
业务层只依赖这个 ABC，换 Milvus / Qdrant 只改实现与工厂函数。

**两种会话，两个方向**（第 11 步票 #127 / #132）：

- **读侧**（两个 ``similarity_search*``）收 ``AsyncSession``——检索链路已经在异步栈上，
  向量查询就是一次 ``await db.execute(...)``，检索路径上因此没有任何会卡住事件循环的
  同步调用。
- **写侧**（``add_chunks`` / ``delete_by_document_id``）收 ``DBSession``——索引链路与
  数据面删除（票 #132）已经是 ``AsyncSession``，而控制面删除要到票 #130 才换，两边
  共用同一份实现，差异收在 ``app/db/dual_session.py``。

同一个 ABC 里两种会话不是笔误，是迁移按垂直切片推进的**中间状态**：两侧的调用方不同、
生命周期也不同，合成一个签名只会让「这次该不该 await」变成运行期的事。两个方向刻意
不共用同一个类型：写方法一旦收窄成同步 ``Session``，索引链路就会在迁移里悄悄退回阻塞
事件循环的那一侧；读侧若改收 ``DBSession``，检索链路上又会多一次运行期判断。
"""

from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dual_session import DBSession


class VectorStore(ABC):
    """向量存储能力。"""

    @abstractmethod
    async def add_chunks(self, db: DBSession, chunks: list[dict]) -> None:
        """批量写入切片向量。"""

    @abstractmethod
    async def similarity_search(
        self,
        db: AsyncSession,
        query_vector: list[float],
        *,
        tenant_id: str,
        kb_id: str,
        top_k: int,
    ) -> list[dict]:
        """返回相似度结果，score = 1 - 余弦距离（越大越相关）；
        必须带 tenant_id 与 kb_id 过滤，否则会跨库召回。"""

    @abstractmethod
    async def similarity_search_scope(
        self,
        db: AsyncSession,
        query_vector: list[float],
        *,
        tenant_id: str,
        index_versions: dict[str, str],
        top_k: int,
    ) -> list[dict]:
        """范围版召回：一次覆盖多个知识库，每库带自己的索引版本。

        ``index_versions`` 是「kb_id → 索引版本」的**映射**，不是两个平行数组：
        过滤条件必须是配对的（每对内部 AND、对之间 OR），写成两个独立的 ``IN``
        会匹配出「A 库的 v2」这种不存在的组合——单版本时看不出问题，多版本并存
        时就是越权或脏数据。形状与 ``KeywordSearchProvider.keyword_search`` 一致。

        返回的每行必须**自带 ``kb_id``**：一次跨库召回的结果要能归属到某个库。
        排序口径与单库版一致（score = 1 - 余弦距离），但 ``top_k`` 是**全局**的
        ——不是每库各取 top_k 再合并，那会让不相关的库也各占名额。
        """

    @abstractmethod
    async def delete_by_document_id(
        self, db: DBSession, document_id: str, *, tenant_id: str
    ) -> None:
        """删某文档的全部切片向量；必须带 tenant_id 过滤，否则会删到其他租户的行。"""
