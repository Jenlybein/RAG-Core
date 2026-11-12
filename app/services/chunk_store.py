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

**会话类型是 ``DBSession``**（第 11 步票 #132）：索引链路与数据面删除已经换成
``AsyncSession``，控制面删除要到票 #130 才换，两边共用同一份实现（见
``app/db/dual_session.py`` 的删除条件）。
"""

from __future__ import annotations

from typing import Protocol

from app.db.dual_session import DBSession
from app.providers.keyword_search import KeywordSearchProvider


class ChunkStore(Protocol):
    """切片写入契约：两处存储都满足它，索引编排只认它。"""

    async def add_chunks(self, db: DBSession, chunks: list[dict]) -> None: ...

    async def delete_by_document_id(
        self, db: DBSession, document_id: str, *, tenant_id: str
    ) -> None: ...


class KeywordSearchChunkStore:
    """把关键词检索 Provider 适配成切片写入契约（它不需要数据库会话）。"""

    def __init__(self, provider: KeywordSearchProvider) -> None:
        self._provider = provider

    async def add_chunks(self, db: DBSession, chunks: list[dict]) -> None:
        # 关键词文档由 Provider 自己从 chunk 字典里取检索文本，这里只透传。
        await self._provider.add_chunks(chunks)

    async def delete_by_document_id(
        self, db: DBSession, document_id: str, *, tenant_id: str
    ) -> None:
        await self._provider.delete_by_document_id(document_id, tenant_id=tenant_id)


def build_chunk_stores(
    vector_store: ChunkStore, keyword_search: KeywordSearchProvider
) -> list[ChunkStore]:
    """默认接线：一份切片同时写进向量存储与关键词引擎。

    顺序是契约的一部分：先向量、后关键词，两处都先删旧再写新；任一处失败都由
    调用方回滚 / 置为失败，**索引阶段不做降级**。
    """

    return [vector_store, KeywordSearchChunkStore(keyword_search)]
