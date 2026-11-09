"""文本切分的抽象与「固定窗口」实现。

`size` 策略用 `WindowTextSplitter`：按 token 窗口切，**不认标题、不认表格、不认代码**。
它的定位没变——给没有结构的纯文本一个稳定可预期的切法——但窗口内部从「字符硬切」
换成了按段落/句子回退，所以片不会从一句话中间断开。

**它不再是评测的朴素基线**：基线（纯字符硬切、什么都不懂）已搬进
`scripts/eval_chunking.py`。生产的 `size` 库现在是个体面的切法，评测的对照物
仍在评测侧，两者互不牵制。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.utils.window_splitter import text_pieces


@dataclass(slots=True)
class SplitPiece:
    """一个切片：text 是切片正文，metadata 是切块器产出的结构化信息。"""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TextSplitter(ABC):
    """把一整篇文本切成切片列表。"""

    @abstractmethod
    def split(self, text: str) -> list[SplitPiece]:
        """返回切片列表；空文本返回空列表。"""


class WindowTextSplitter(TextSplitter):
    """按固定 token 窗口切，不认识任何结构。

    切片不带 metadata：没有章节边界，也就没有 `heading_path` / `section_id`。
    `size` 库「不承诺章节溯源」这条边界，正是由这里决定的。
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 50) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[SplitPiece]:
        return [
            SplitPiece(text=piece)
            for piece in text_pieces(
                text, size=self.chunk_size, overlap=self.chunk_overlap
            )
        ]
