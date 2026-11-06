"""解析器注册表：按文件名 / MIME 选第一个 supports() 为真的解析器。"""

from app.core.exceptions import AppError, ErrorCode
from app.providers.parsers.base import DocumentParser, ParsedDocument


class ParserRegistry:
    def __init__(self, parsers: list[DocumentParser]) -> None:
        self._parsers = list(parsers)

    def parse(self, file_bytes: bytes, *, filename: str, mime: str = "") -> ParsedDocument:
        for parser in self._parsers:
            if parser.supports(filename, mime):
                return parser.parse(file_bytes, filename=filename)
        raise AppError(ErrorCode.DOCUMENT_PARSE_FAILED, f"no parser supports: {filename}")
