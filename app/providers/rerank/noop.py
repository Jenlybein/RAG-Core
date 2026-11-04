"""空实现：原样返回前 top_n 条，不排序也不给 rerank_score。"""

from app.providers.rerank.base import RerankProvider


class NoopRerankProvider(RerankProvider):
    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        return chunks[:top_n]