"""空实现：不支持任何格式，用于表达「解析能力缺失」。"""

from app.providers.parsers.base import DocumentParser


class NoopParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return False

    def parse(self, file_bytes: bytes, *, filename: str) -> str:
        raise NotImplementedError("no parser supports this file")