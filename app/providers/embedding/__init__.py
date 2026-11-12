"""Embedding 能力出口：抽象 + 工厂。业务层写 get_embedding_provider()。

两套实现用 ``EMBEDDING_PROVIDER`` 切换：

- ``langchain``（默认）：走框架的 ``Embeddings`` 抽象（ADR-0011 的边界）；
- ``openai_compatible``：第 1 步就有的直连实现，作为回退分支**原样保留**——
  它是「框架这一层出问题时还能把服务救回来」的那条路，不是历史遗留。

配置写错要立刻炸而不是静默换一个实现：换了一台机器行为就变，是最难查的一类故障。

**按配置指纹缓存**（第 10 步票 4）：改造前每条检索都现装一套嵌入实现，连带两个新建的
``httpx`` 客户端（一次 TLS 握手 + 实测约 43 ms 的代理环境探测）。缓存键是「真正决定它
打什么请求」的那几个字段，配置一改就换实例；缓存里**只有 Provider**，模型与客户端按
调用现取（见 ``app/core/http_clients.py``）。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import OpenAIEmbeddings

from app.core.config import settings
from app.core.http_clients import model_traffic_client_options
from app.providers.embedding.base import EmbeddingProvider
from app.providers.embedding.langchain_embeddings import LangChainEmbedding
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbedding
from app.providers.langchain_runtime import apply_framework_telemetry_switches

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
#
# 它同时进缓存键：换了预算就是换了一份「这一路怎么超时」的约定，旧实例不该被复用。
_EMBEDDING_TIMEOUT_SECONDS = 60

#: 已装配过的 Provider，按「实现 + 端点指纹 + 批次 / 维度 + 超时」做键。
_PROVIDER_CACHE: dict[tuple[Any, ...], EmbeddingProvider] = {}


def reset_embedding_provider_cache() -> None:
    """清掉缓存。**只给测试用**：进程级缓存不该在用例之间串味。"""

    _PROVIDER_CACHE.clear()


def get_embedding_provider() -> EmbeddingProvider:
    # 遥测开关是框架级设置，与「用哪套嵌入实现」无关；两个构造点都要调用它，
    # 否则「没配 key 也要关掉外发」这条审计证据会在最需要它的环境下缺席。
    apply_framework_telemetry_switches()

    mode = settings.EMBEDDING_PROVIDER.strip().lower()
    if mode == _MODE_LANGCHAIN:
        key = _langchain_key()
        provider = _PROVIDER_CACHE.get(key)
        if provider is None:
            provider = LangChainEmbedding(
                # 装配动作，不是一个对象：模型每次现装，连接池来自注册表（见模块文档）。
                embeddings_factory=_build_openai_embeddings,
                batch_size=settings.EMBEDDING_BATCH_SIZE,
                dimensions=settings.EMBEDDING_DIMENSIONS,
            )
            _PROVIDER_CACHE[key] = provider
        return provider
    if mode == _MODE_OPENAI_COMPATIBLE:
        key = _fallback_key()
        provider = _PROVIDER_CACHE.get(key)
        if provider is None:
            provider = OpenAICompatibleEmbedding(
                base_url=settings.MODEL_BASE_URL,
                api_key=settings.MODEL_API_KEY,
                model=settings.EMBEDDING_MODEL,
                dimensions=settings.EMBEDDING_DIMENSIONS,
                batch_size=settings.EMBEDDING_BATCH_SIZE,
            )
            _PROVIDER_CACHE[key] = provider
        return provider
    raise ValueError(
        f"未知的嵌入实现 {settings.EMBEDDING_PROVIDER!r}，只支持 "
        f"{(_MODE_LANGCHAIN, _MODE_OPENAI_COMPATIBLE)}"
    )


def _langchain_key() -> tuple[Any, ...]:
    """框架实现的指纹：地址 / key / 模型 / 维度 / 批次 / 超时就是它的全部输入。"""

    return (
        _MODE_LANGCHAIN,
        settings.MODEL_BASE_URL,
        settings.MODEL_API_KEY,
        settings.EMBEDDING_MODEL,
        settings.EMBEDDING_DIMENSIONS,
        settings.EMBEDDING_BATCH_SIZE,
        _EMBEDDING_TIMEOUT_SECONDS,
    )


def _fallback_key() -> tuple[Any, ...]:
    """直连实现的指纹：与框架实现各用一张键，换实现就是换一个对象。"""

    return (
        _MODE_OPENAI_COMPATIBLE,
        settings.MODEL_BASE_URL,
        settings.MODEL_API_KEY,
        settings.EMBEDDING_MODEL,
        settings.EMBEDDING_DIMENSIONS,
        settings.EMBEDDING_BATCH_SIZE,
    )


def _build_openai_embeddings() -> OpenAIEmbeddings:
    """百炼走 OpenAI 兼容协议，所以这里用 OpenAI 的嵌入实现。

    ``check_embedding_ctx_length=False`` 是等价性的前提：开着时框架会先按 tiktoken
    切 token 再拼批，同一段文本在两套实现里送出去的输入就不一样了。

    连接来自注册表（``model_traffic_client_options``）：框架在构造时**即使拿到异步客户端**
    也会顺手建一个同步 ``httpx.Client``，所以两个都要给，否则每次装配白付一次约 43 ms 的
    构造代价（见 ``app/core/http_clients.py``）。
    """

    options: dict[str, object] = {
        "model": settings.EMBEDDING_MODEL,
        "dimensions": settings.EMBEDDING_DIMENSIONS,
        "chunk_size": settings.EMBEDDING_BATCH_SIZE,
        "api_key": settings.MODEL_API_KEY or _UNCONFIGURED_KEY,
        "check_embedding_ctx_length": False,
        "timeout": _EMBEDDING_TIMEOUT_SECONDS,
        **model_traffic_client_options(),
    }
    if settings.MODEL_BASE_URL:
        options["base_url"] = settings.MODEL_BASE_URL
    return OpenAIEmbeddings(**options)  # type: ignore[arg-type]


__all__ = [
    "EmbeddingProvider",
    "get_embedding_provider",
    "reset_embedding_provider_cache",
]
