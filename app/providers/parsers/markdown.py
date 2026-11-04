"""Markdown 解析器：骨架期唯一实现（上传接口本步只收 JSON，暂不走它）。"""

from app.providers.parsers.base import DocumentParser


class MarkdownParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return filename.lower().endswith((".md", ".markdown")) or mime in ("text/markdown", "text/x-markdown")

    def parse(self, file_bytes: bytes, *, filename: str) -> str:
        return file_bytes.decode("utf-8")