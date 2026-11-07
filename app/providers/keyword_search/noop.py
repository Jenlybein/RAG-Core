"""空实现：显式表达「关键词检索能力不存在」。

它照常实现写入与删除（安静地什么都不做），所以索引编排的存储列表里可以永远
摆着它；但 ``available`` 为 ``False``，``mode=bm25`` 会因此硬失败，而不是拿一个
空列表冒充「查过了、没命中」。
"""

from app.providers.keyword_search.base import KeywordSearchProvider


class NoopKeywordSearchProvider(KeywordSearchProvider):
    @property
    def available(self) -> bool:
        return False

    async def add_chunks(self, chunks: list[dict]) -> None:
        return None

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: dict[str, str], top_k: int
    ) -> list[dict]:
        return []

    async def delete_by_document_id(self, document_id: str, *, tenant_id: str) -> None:
        return None
