"""关键词检索能力出口：抽象 + 按 ``KEYWORD_SEARCH_PROVIDER`` 选择的工厂。

``none`` → 空实现（离线 / 没部署 ES 的机器，服务照常启动，能力明确不可用）；
``elasticsearch`` → ES 实现；其余取值直接 ``ValueError``——拼错的名字不能悄悄
退回空实现，否则「配置了但检索静默不生效」会成为最难查的一类故障。
"""

from app.core.config import settings
from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider

__all__ = [
    "KeywordSearchProvider",
    "NoopKeywordSearchProvider",
    "get_keyword_search_provider",
]


def get_keyword_search_provider() -> KeywordSearchProvider:
    name = settings.KEYWORD_SEARCH_PROVIDER.strip().lower()

    if name == "none":
        return NoopKeywordSearchProvider()

    if name == "elasticsearch":
        # 惰性 import：none 路径不加载 ES 客户端，也不构造它的传输层。
        from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider

        return ElasticsearchKeywordSearchProvider(
            url=settings.ELASTICSEARCH_URL,
            index_name=settings.ELASTICSEARCH_INDEX,
            # 时间预算在这里注入（组合根读配置），Provider 自己不读全局配置。
            timeout_s=settings.KEYWORD_SEARCH_TIMEOUT_S,
        )

    raise ValueError(
        f"unknown KEYWORD_SEARCH_PROVIDER: {settings.KEYWORD_SEARCH_PROVIDER!r} "
        "(expected 'none' or 'elasticsearch')"
    )
