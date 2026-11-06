"""Markdown 解析器：阶段 3 的多格式解析之一。"""

from app.providers.parsers.base import DocumentParser, ParsedDocument


class MarkdownParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return filename.lower().endswith((".md", ".markdown")) or mime in ("text/markdown", "text/x-markdown")

    def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
        return ParsedDocument(
            content=file_bytes.decode("utf-8"),
            source_type="markdown",
            metadata={"parser": "markdown"},
        )
