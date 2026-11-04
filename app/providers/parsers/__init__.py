"""解析能力出口：注册表（Markdown + Noop 兜底）。"""

from app.providers.parsers.base import DocumentParser
from app.providers.parsers.markdown import MarkdownParser
from app.providers.parsers.noop import NoopParser
from app.providers.parsers.registry import ParserRegistry


def get_parser_registry() -> ParserRegistry:
    return ParserRegistry([MarkdownParser(), NoopParser()])


__all__ = ["DocumentParser", "ParserRegistry", "get_parser_registry"]