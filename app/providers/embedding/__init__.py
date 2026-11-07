"""Embedding 能力出口：抽象 + 工厂。业务层写 get_embedding_provider()。

两套实现用 ``EMBEDDING_PROVIDER`` 切换：

- ``langchain``（默认）：走框架的 ``Embeddings`` 抽象（ADR-0011 的边界）；
- ``openai_compatible``：第 1 步就有的直连实现，作为回退分支**原样保留**——
  它是「框架这一层出问题时还能把服务救回来」的那条路，不是历史遗留。

配置写错要立刻炸而不是静默换一个实现：换了一台机器行为就变，是最难查的一类故障。
"""

from __future__ import annotations

from langchain_openai import OpenAIEmbeddings

from app.core.config import settings
from app.providers.embedding.base import EmbeddingProvider
from app.providers.embedding.langchain_embeddings import LangChainEmbedding
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbedding

_MODE_LANGCHAIN = "langchain"
_MODE_OPENAI_COMPATIBLE = "openai_compatible"

# 没配 key 时放进构造参数里的占位值。
#
# 为什么不用空串：框架的 OpenAI 客户端在**构造期**就校验凭据，空 key 会让
# get_embedding_provider() 直接抛异常——而离线环境（CI、没配 .env 的开发机）必须能
# 启动，甚至在检索链路里走到「嵌入失败」这个可预期的分支上。真缺 key 的失败留到
# 第一次请求，和旧直连实现的行为一致。
_UNCONFIGURED_KEY = "unconfigured"

# 嵌入请求的超时。与旧直连实现的 60 秒一致：批量嵌入一篇长文档往往要几十秒。
_EMBEDDING_TIMEOUT_SECONDS = 60


def get_embedding_provider() -> EmbeddingProvider:
    mode = settings.EMBEDDING_PROVIDER.strip().lower()
    if mode == _MODE_LANGCHAIN:
        return LangChainEmbedding(
            embeddings=_build_openai_embeddings(),
            batch_size=settings.EMBEDDING_BATCH_SIZE,
            dimensions=settings.EMBEDDING_DIMENSIONS,
        )
    if mode == _MODE_OPENAI_COMPATIBLE:
        return OpenAICompatibleEmbedding(
            base_url=settings.MODEL_BASE_URL,
            api_key=settings.MODEL_API_KEY,
            model=settings.EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIMENSIONS,
            batch_size=settings.EMBEDDING_BATCH_SIZE,
        )
    raise ValueError(
        f"未知的嵌入实现 {settings.EMBEDDING_PROVIDER!r}，只支持 "
        f"{(_MODE_LANGCHAIN, _MODE_OPENAI_COMPATIBLE)}"
    )


def _build_openai_embeddings() -> OpenAIEmbeddings:
    """百炼走 OpenAI 兼容协议，所以这里用 OpenAI 的嵌入实现。

    ``check_embedding_ctx_length=False`` 是等价性的前提：开着时框架会先按 tiktoken
    切 token 再拼批，同一段文本在两套实现里送出去的输入就不一样了。
    """

    options: dict[str, object] = {
        "model": settings.EMBEDDING_MODEL,
        "dimensions": settings.EMBEDDING_DIMENSIONS,
        "chunk_size": settings.EMBEDDING_BATCH_SIZE,
        "api_key": settings.MODEL_API_KEY or _UNCONFIGURED_KEY,
        "check_embedding_ctx_length": False,
        "timeout": _EMBEDDING_TIMEOUT_SECONDS,
    }
    if settings.MODEL_BASE_URL:
        options["base_url"] = settings.MODEL_BASE_URL
    return OpenAIEmbeddings(**options)  # type: ignore[arg-type]


__all__ = ["EmbeddingProvider", "get_embedding_provider"]
