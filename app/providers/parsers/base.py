"""DocumentParser 抽象：阶段 3 补 docx / pdf。

纯 CPU 活（读字节、解码），用同步方法，不套 async。
"""

from abc import ABC, abstractmethod


class DocumentParser(ABC):
    @abstractmethod
    def supports(self, filename: str, mime: str = "") -> bool: ...

    @abstractmethod
    def parse(self, file_bytes: bytes, *, filename: str) -> str: ...