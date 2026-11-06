"""DocumentParser 抽象：阶段 3 补 docx / pdf。

解析是纯 CPU 活（读字节、解码 / 转 HTML / 抽文本），所以 ``parse`` 保持同步，
不套 async：只有 embedding 才走网络。反过来说，如果某个解析器开始调用托管
OCR 服务，就要重新评估 async 或 ``run_in_executor``。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ParsedDocument:
    """解析结果：切分器的唯一输入是 ``content``。"""

    content: str
    source_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentParser(ABC):
    @abstractmethod
    def supports(self, filename: str, mime: str = "") -> bool: ...

    @abstractmethod
    def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument: ...
