"""空实现：``RERANK_ENABLED=false``（默认）时工厂返回它。

它原样返回前 ``top_n`` 条、不排序、不给重排分——「没有这一步」而不是「排了一遍顺序
恰好没变」。``available=False`` 让编排层连调用都省掉，日志与耗时因此也如实为空。
"""

from app.providers.rerank.base import RerankProvider


class NoopRerankProvider(RerankProvider):
    @property
    def available(self) -> bool:
        return False

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        return chunks[:top_n]
