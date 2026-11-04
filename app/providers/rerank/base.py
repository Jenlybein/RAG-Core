"""RerankProvider 抽象（阶段 8 才有 Qwen3.7 实现）。"""

from abc import ABC, abstractmethod


class RerankProvider(ABC):
    @abstractmethod
    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]: ...