"""空实现：显式表达「关键词检索能力不存在」。阶段 4 换 ES 实现时业务层零改动。"""

from app.providers.keyword_search.base import KeywordSearchProvider


class NoopKeywordSearchProvider(KeywordSearchProvider):
    async def add_chunks(self, chunks: list[dict]) -> None:
        return None

    async def keyword_search(self, query: str, *, tenant_id: str, kb_id: str, top_k: int) -> list[dict]:
        return []

    async def delete_by_document_id(self, document_id: str) -> None:
        return None