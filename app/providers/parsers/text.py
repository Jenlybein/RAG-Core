"""纯文本解析器：UTF-8 解码，不做任何改写。"""

from app.providers.parsers.base import DocumentParser, ParsedDocument


class TextParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return filename.lower().endswith(".txt") or mime == "text/plain"

    def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
        return ParsedDocument(
            content=file_bytes.decode("utf-8"),
            source_type="text",
            metadata={"parser": "text"},
        )
