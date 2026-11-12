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


#: 已构造过的 Provider，按「真正决定它身份的那几个配置」做键。
#:
#: 为什么必须缓存（一次实测）：``ElasticsearchKeywordSearchProvider`` 内部惰性创建一个
#: ``AsyncElasticsearch``，而它的连接池**从不归还**——原来每次检索都调本函数，于是
#: 每一题都新建一个客户端、每一题都重做一次 TLS 握手，进程结束时刷出一串
#: ``Unclosed client session`` / ``Unclosed connector``。50 题的旁证跑批因此攒下 50 个
#: 悬空客户端，噪音还盖住了真正要看的日志。
#:
#: 键里带上 url / index / timeout：运行时改了其中任何一个，拿到的就是另一个实例，
#: 不会出现「配置改了但连接还指着旧地址」。
_PROVIDER_CACHE: dict[tuple[str, str, str, float], KeywordSearchProvider] = {}


def reset_keyword_search_provider_cache() -> None:
    """清掉缓存。**只给测试用**：进程级缓存不该在用例之间串味。"""

    _PROVIDER_CACHE.clear()


def get_keyword_search_provider() -> KeywordSearchProvider:
    name = settings.KEYWORD_SEARCH_PROVIDER.strip().lower()
    cache_key = (
        name,
        settings.ELASTICSEARCH_URL,
        settings.ELASTICSEARCH_INDEX,
        settings.KEYWORD_SEARCH_TIMEOUT_S,
    )
    cached = _PROVIDER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if name == "none":
        provider: KeywordSearchProvider = NoopKeywordSearchProvider()
    elif name == "elasticsearch":
        # 惰性 import：none 路径不加载 ES 客户端，也不构造它的传输层。
        from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider

        provider = ElasticsearchKeywordSearchProvider(
            url=settings.ELASTICSEARCH_URL,
            index_name=settings.ELASTICSEARCH_INDEX,
            # 时间预算在这里注入（组合根读配置），Provider 自己不读全局配置。
            timeout_s=settings.KEYWORD_SEARCH_TIMEOUT_S,
        )
    else:
        raise ValueError(
            f"unknown KEYWORD_SEARCH_PROVIDER: {settings.KEYWORD_SEARCH_PROVIDER!r} "
            "(expected 'none' or 'elasticsearch')"
        )

    _PROVIDER_CACHE[cache_key] = provider
    return provider
