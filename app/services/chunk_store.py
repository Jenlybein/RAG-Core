"""切片存储的写入契约：向量存储与关键词检索在这一层被同一份代码遍历。

索引编排只认这两个方法（``add_chunks`` / ``delete_by_document_id``），不认具体
存储类型；将来加第三个存储（比如说图数据库的切片索引）只要再实现一次这两个
方法、往默认接线里塞一个元素，调用方一行都不用改。

**为什么要一个适配器**：向量存储的写入天然需要数据库会话（它写的行要和其他
业务行在同一个事务里提交），关键词引擎却把文档发到 ES、与数据库事务无关。
把「要不要 db」这个差异关在适配器里，编排层就只看到一种形状——否则每加一个
存储，编排里就要多一条 if 分支，而这正是票 23 要消灭的东西。

``ChunkStore`` 是协议而不是基类：向量存储有自己的 ABC（换实现不该被绑定到
本模块），只要形状对上就是合法的存储。

**会话类型是 ``AsyncSession``**（第 11 步票 #130）：索引链路、数据面删除与控制面删除
都已经是异步栈，迁移期的「两种会话都喂」已经收窄成这一种。
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.keyword_search import KeywordSearchProvider


class ChunkStore(Protocol):
    """切片写入契约：两处存储都满足它，索引编排只认它。

    两个删除范围，**顺序就是语义边界**：

    ``doc_version`` 是**第一个**（#173）：给了稿号只清那一稿（重建第 2 稿时第 1 稿的切片
    原样留着——「每稿各留切片」），``None`` 清这篇文档所有稿（删文档）。

    ``generation`` 是**第二个**（现场九·第五步）：给了代就只清那一代。重建时写的是
    **新一代**，所以这一条让「上一代」留在库里给单篇回滚用（保留最近 2 代，夜间回收）。

    没有库级版本范围：``chunks.index_version`` 那一列与服务它的版本层一起删了；可见性
    只由「代 + 稿」两轴表达。

    关键词那一路**两个范围都不认**：它是镜像（每篇只有一份），清理永远按文档全清，
    见 ``KeywordSearchChunkStore``。
    """

    async def add_chunks(self, db: AsyncSession, chunks: list[dict]) -> None: ...

    async def delete_by_document_id(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        tenant_id: str,
        doc_version: int | None = None,
        generation: int | None = None,
    ) -> None: ...


class KeywordSearchChunkStore:
    """把关键词检索 Provider 适配成切片写入契约（它不需要数据库会话）。"""

    def __init__(self, provider: KeywordSearchProvider) -> None:
        self._provider = provider

    async def add_chunks(self, db: AsyncSession, chunks: list[dict]) -> None:
        # 关键词文档由 Provider 自己从 chunk 字典里取检索文本，这里只透传。
        await self._provider.add_chunks(chunks)

    async def delete_by_document_id(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        tenant_id: str,
        doc_version: int | None = None,
        generation: int | None = None,
    ) -> None:
        # 关键词侧是**镜像**，不是另一个存储（现场九·第五步）：既没有「代」这一维，也不保留
        # 多份。所以只往下传稿号：
        #
        # * ``generation``：这一侧没有代轴，重建就先按文档清空再写新的一份；
        # * 也没有库级版本：物理索引只有一个（配置里的那个名字），版本化物理索引与别名
        #   切换已随库级版本层删除。
        #
        # 代价说清楚：归档稿（同稿的旧产物）在关键词侧不保留。关键词侧本来就不为归档产物
        # 服务（读侧只认当前镜像），这正是「镜像」的含义。
        del generation
        await self._provider.delete_by_document_id(
            document_id,
            tenant_id=tenant_id,
            doc_version=doc_version,
        )


def build_chunk_stores(
    vector_store: ChunkStore, keyword_search: KeywordSearchProvider
) -> list[ChunkStore]:
    """默认接线：一份切片同时写进向量存储与关键词引擎。

    顺序是契约的一部分：先向量、后关键词，两处都先删旧再写新；任一处失败都由
    调用方回滚 / 置为失败，**索引阶段不做降级**。
    """

    return [vector_store, KeywordSearchChunkStore(keyword_search)]
