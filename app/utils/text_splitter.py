"""文本切分：抽象 + 骨架期的字符切实现。

按 ADR-0002 的判据，切分器没有自己的外部依赖，不算六类 Provider 之一，
所以放在 utils/ 而不是 providers/。但它仍抽 ABC：阶段 3 会整体换成
MarkdownStructuredSplitter，业务层只依赖这个抽象。

CHUNK_SIZE=800 / CHUNK_OVERLAP=100 是骨架期占位值，按字符切，不认标题和表格。
"""

from abc import ABC, abstractmethod


class TextSplitter(ABC):
    """把一整篇文本切成切片正文列表。"""

    @abstractmethod
    def split(self, text: str) -> list[str]:
        """返回切片正文；空文本返回空列表。"""


class CharacterTextSplitter(TextSplitter):
    """按固定字符数切，相邻切片保留 overlap 个字符。

    骨架期唯一实现。阶段 3 换成结构化切片时，IndexingService 的调用不变。
    """

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[str]:
        if not text:
            return []
        step = self.chunk_size - self.chunk_overlap
        chunks: list[str] = []
        start = 0
        while start < len(text):
            chunks.append(text[start : start + self.chunk_size])
            start += step
        return chunks
