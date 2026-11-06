"""DOCX 解析器：mammoth 转 HTML → markdownify 转 Markdown。

``heading_style="ATX"`` 是**必须**的：markdownify 默认输出 Setext 标题
（``标题`` 下一行 ``======``），而结构化切块器只认 ``#`` / ``##``；用默认值会让
每一章标题退化成正文，切块器再也切不出章节边界。
"""

import io

import mammoth
from markdownify import markdownify

from app.core.exceptions import AppError, ErrorCode
from app.providers.parsers.base import DocumentParser, ParsedDocument


class DocxParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return filename.lower().endswith(".docx") or mime == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
        result = mammoth.convert_to_html(io.BytesIO(file_bytes))
        content = markdownify(result.value, heading_style="ATX")
        if not content.strip():
            raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "docx 中没有可提取文字")

        # mammoth 的 messages 是 (type, message) 命名元组；拍平成字符串进 metadata，
        # 避免把上游对象带进可 JSON 序列化的元数据里。
        warnings = [f"{message.type}: {message.message}" for message in result.messages]
        return ParsedDocument(
            content=content,
            source_type="docx",
            metadata={"parser": "docx", "warnings": warnings},
        )
