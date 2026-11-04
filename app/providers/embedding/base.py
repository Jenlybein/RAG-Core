"""EmbeddingProvider 抽象。

embed_texts（批量、要分批与重试）与 embed_query（单条、要低延迟）
是两个方法而不是一个方法加参数——两侧的扩展方向不同。
"""

from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """把文本变成向量的能力。"""

    @abstractmethod
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文档切片。"""

    @abstractmethod
    async def embed_query(self, query: str) -> list[float]:
        """单条向量化查询，低延迟优先。"""