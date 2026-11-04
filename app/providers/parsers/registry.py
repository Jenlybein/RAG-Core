"""解析器注册表：按文件名 / MIME 选第一个 supports() 为真的解析器。"""

from app.providers.parsers.base import DocumentParser


class ParserRegistry:
    def __init__(self, parsers: list[DocumentParser]) -> None:
        self._parsers = list(parsers)

    def parse(self, file_bytes: bytes, *, filename: str, mime: str = "") -> str:
        for parser in self._parsers:
            if parser.supports(filename, mime):
                return parser.parse(file_bytes, filename=filename)
        raise ValueError(f"no parser supports: {filename}")