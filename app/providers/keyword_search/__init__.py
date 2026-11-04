"""关键词检索能力出口。"""

from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider


def get_keyword_search_provider() -> KeywordSearchProvider:
    return NoopKeywordSearchProvider()


__all__ = ["KeywordSearchProvider", "get_keyword_search_provider"]