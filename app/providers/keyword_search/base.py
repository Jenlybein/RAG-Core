"""KeywordSearchProvider 抽象（阶段 4 才实现 ES 版）。"""

from abc import ABC, abstractmethod


class KeywordSearchProvider(ABC):
    @abstractmethod
    async def add_chunks(self, chunks: list[dict]) -> None: ...

    @abstractmethod
    async def keyword_search(self, query: str, *, tenant_id: str, kb_id: str, top_k: int) -> list[dict]: ...

    @abstractmethod
    async def delete_by_document_id(self, document_id: str) -> None: ...