"""解析能力出口：4 个解析器 + 注册表。

注册表里 Docx / Pdf 排在前面：具体格式先试，避免教程里警告的「宽松
``supports()`` 抢先匹配」。``NoopParser`` 保留可用，但**不放进注册表**——没有
解析器匹配时，注册表本来就抛 ``DOCUMENT_PARSE_FAILED``，无需兜底对象。
"""

from app.providers.parsers.base import DocumentParser, ParsedDocument
from app.providers.parsers.docx import DocxParser
from app.providers.parsers.markdown import MarkdownParser
from app.providers.parsers.noop import NoopParser
from app.providers.parsers.pdf import PdfParser
from app.providers.parsers.registry import ParserRegistry
from app.providers.parsers.text import TextParser


def get_parser_registry() -> ParserRegistry:
    return ParserRegistry([DocxParser(), PdfParser(), TextParser(), MarkdownParser()])


__all__ = [
    "DocumentParser",
    "DocxParser",
    "MarkdownParser",
    "NoopParser",
    "ParsedDocument",
    "ParserRegistry",
    "PdfParser",
    "TextParser",
    "get_parser_registry",
]
