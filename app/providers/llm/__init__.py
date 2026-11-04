"""大模型能力出口。"""

from app.providers.llm.base import LLMProvider
from app.providers.llm.noop import NoopLLMProvider


def get_llm_provider() -> LLMProvider:
    return NoopLLMProvider()


__all__ = ["LLMProvider", "get_llm_provider"]