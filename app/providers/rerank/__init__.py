"""重排能力出口。"""

from app.providers.rerank.base import RerankProvider
from app.providers.rerank.noop import NoopRerankProvider


def get_rerank_provider() -> RerankProvider:
    return NoopRerankProvider()


__all__ = ["RerankProvider", "get_rerank_provider"]