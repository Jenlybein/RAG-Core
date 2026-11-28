"""VectorStore 抽象。

判据：向量写入与相似度检索归它，普通 CRUD 归 ChunkRepository。
业务层只依赖这个 ABC，换 Milvus / Qdrant 只改实现与工厂函数。

**所有方法的会话类型都是 ``AsyncSession``**（第 11 步票 #127 / #130 / #132）：

- **读侧**（``similarity_search`` / ``similarity_search_scope`` /
  ``similarity_search_document_version``）：向量查询就是一次 ``await db.execute(...)``，
  检索路径上没有任何会卡住事件循环的同步调用；
- **写侧**（``add_chunks`` / ``delete_by_document_id``）：索引链路、数据面删除与控制面
  删除都在异步栈上，迁移期那个「同步与异步都喂」的联合类型已经收窄成这一种。

类型只剩一种不是简化设计，而是迁移走完那一刀的**结果**：写侧一旦退回同步 ``Session``，
索引链路就会悄悄重新阻塞事件循环。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession


class VectorStore(ABC):
    """向量存储能力。"""

    @abstractmethod
    async def add_chunks(self, db: AsyncSession, chunks: list[dict]) -> None:
        """批量写入切片向量。

        每个切片的键里带 ``generation``（这一片属于该篇的第几代产物）：**调用方必须给**。
        缺键时实现会落 ``DEFAULT_GENERATION``（第 1 代，与迁移回填同源）——那是个合法代，
        写错不报错，只表现为「这篇永远查不到新代」或「旧代冒充当前代」。
        """

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
        kb_ids: Sequence[str],
        top_k: int,
    ) -> list[dict]:
        """范围版召回：一次覆盖多个知识库。

        ``kb_ids`` 是**库集合**，可见性不在这里判（现场九·第四步）：哪一代产物在服务由
        ``current_generation_filter()`` 交 JOIN 的文档行求值，当前稿同理。库级索引版本
        那一层（「kb_id → 索引版本」的配对映射）已经整层删除，这里没有版本可传。

        返回的每行必须**自带 ``kb_id``**：一次跨库召回的结果要能归属到某个库。
        排序口径与单库版一致（score = 1 - 余弦距离），但 ``top_k`` 是**全局**的
        ——不是每库各取 top_k 再合并，那会让不相关的库也各占名额。
        """

    @abstractmethod
    async def similarity_search_document_version(
        self,
        db: AsyncSession,
        query_vector: list[float],
        *,
        tenant_id: str,
        document_id: str,
        doc_version: int,
        generation: int,
        top_k: int,
    ) -> list[dict]:
        """**锁定某篇某稿**的召回：只返回这一篇文档这一稿这一代的切片（#176）。

        与 :meth:`similarity_search_scope` 的两处差别都是刻意的：

        * 它是「按版本只读直查」的底层——排查「旧版明明写了、新版为什么召回不到」，
          所以**必须绕过**「只看当前稿」谓词（``current_draft_filter``）：那是用
          ``chunks.doc_version = documents.current_version`` 做的列与列比较，历史稿一片
          都对不上，不绕过去直查永远是空结果；
        * 范围由**文档 + 稿号 + 索引代**三件确定，而不是库级版本映射：
          一次只看一篇文档，库是它的库。

        ``tenant_id`` 照样必带（ADR-0006）：少了它就是把别的租户的片召回来。
        ``generation`` 也必带——同一稿可以有多代产物，不按代收窄会把两次重建的
        结果混在一起。

        **默认路径的语义不受影响**：不传稿号的召回仍走 :meth:`similarity_search_scope`，
        仍然只看当前稿。多出来的这个方法是额外的一条路，不是把老路改宽。
        """

    @abstractmethod
    async def delete_by_document_id(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        tenant_id: str,
        doc_version: int | None = None,
        generation: int | None = None,
    ) -> None:
        """删某文档的切片向量；必须带 tenant_id 过滤，否则会删到其他租户的行。

        ``doc_version`` 是**稿号范围**（#173）：给了稿号只删那一稿（重建第 2 稿绝不碰
        第 1 稿的切片），``None`` 删这篇文档的所有稿（删文档那条老路径）。

        ``generation`` 是**代的范围**（现场九·第五步）：重建写的是新一代，只清这一代就
        让上一代原样留着给回滚。没有库级版本范围：``chunks.index_version`` 列已随版本层
        删除，可见性只由代表达。
        """
