"""嵌入 Provider：工厂选择 + LangChain 实现的分批与错误契约。

全部离线：工厂选型只构造对象，不发请求；分批行为用替身 Embeddings 观察「发了几次、
每次几条、回来还是不是原来的顺序」——这三件事才是嵌入等价性的前提。

旧直连实现 ``openai_compatible`` 是回退分支，一个字都没动：它仍被
``tests/test_contracts.py`` 直接 import，回到它必须随时可用。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.embeddings import Embeddings

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding import get_embedding_provider
from app.providers.embedding.langchain_embeddings import LangChainEmbedding
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbedding


class _RecordingEmbeddings(Embeddings):
    """记下每次批次的大小，并按文本长度造可区分的向量。"""

    def __init__(self, *, dimensions: int = 3, fail: bool = False) -> None:
        self.batch_sizes: list[int] = []
        self.queries: list[str] = []
        self._dimensions = dimensions
        self._fail = fail

    def _vector(self, text: str) -> list[float]:
        if self._fail:
            raise RuntimeError("upstream 401")
        return [float(len(text))] * self._dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [self._vector(text) for text in texts]

    async def aembed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)

    async def aembed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)


def test_factory_defaults_to_the_langchain_implementation() -> None:
    """默认走框架抽象；默认值本身就是「新实现可用」的表态。"""

    provider = get_embedding_provider()

    assert isinstance(provider, LangChainEmbedding)
    assert settings.EMBEDDING_PROVIDER == "langchain"


def test_factory_passes_model_dimensions_and_batch_size_through() -> None:
    """dimensions 没被透传的话，服务端会按自己的默认维度返回，检索直接对不上号。"""

    provider = get_embedding_provider()
    embeddings = provider.embeddings

    assert getattr(embeddings, "model", None) == settings.EMBEDDING_MODEL
    assert getattr(embeddings, "dimensions", None) == settings.EMBEDDING_DIMENSIONS
    assert getattr(embeddings, "chunk_size", None) == settings.EMBEDDING_BATCH_SIZE


def test_factory_can_fall_back_to_the_direct_http_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回退路径必须一直可用：新实现出问题时靠它把服务救回来。"""

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai_compatible")

    assert isinstance(get_embedding_provider(), OpenAICompatibleEmbedding)


def test_factory_rejects_unknown_implementation_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写错要立刻炸，不能静默换一个实现——那会变成「换了一台服务器行为就变了」。"""

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "langchian")

    with pytest.raises(ValueError):
        get_embedding_provider()


def test_documents_are_batched_and_keep_their_order() -> None:
    """10 条文本按 batch_size 分批发，回来仍是入参顺序——顺序错了就是向量张冠李戴。"""

    embeddings = _RecordingEmbeddings()
    provider = LangChainEmbedding(embeddings=embeddings, batch_size=4)
    texts = [f"text-{i}" for i in range(10)]

    vectors = asyncio.run(provider.embed_texts(texts))

    assert embeddings.batch_sizes == [4, 4, 2]
    assert [vector[0] for vector in vectors] == [float(len(text)) for text in texts]


def test_query_embedding_takes_the_single_text_path() -> None:
    """查询是单条、低延迟那条路，不该混进批量分批逻辑。"""

    embeddings = _RecordingEmbeddings()
    provider = LangChainEmbedding(embeddings=embeddings, batch_size=4)

    vector = asyncio.run(provider.embed_query("错误码 E1024"))

    assert embeddings.queries == ["错误码 E1024"]
    # "错误码 E1024" 共 9 个字符，替身按字符数造向量。
    assert vector == [9.0, 9.0, 9.0]


def test_embedding_failure_is_mapped_to_embedding_failed() -> None:
    """上游失败要带业务码，不能把 httpx / openai 的异常原样冒给调用方。"""

    provider = LangChainEmbedding(embeddings=_RecordingEmbeddings(fail=True), batch_size=4)

    with pytest.raises(AppError) as excinfo:
        asyncio.run(provider.embed_texts(["a", "b"]))

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED


def test_dimension_drift_is_rejected() -> None:
    """服务端悄悄换了维度（换个模型 / 漏传 dimensions）时要当场炸，而不是写进向量库。"""

    provider = LangChainEmbedding(
        embeddings=_RecordingEmbeddings(dimensions=3), batch_size=4, dimensions=1536
    )

    with pytest.raises(AppError) as excinfo:
        asyncio.run(provider.embed_texts(["a"]))

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED


def test_short_batch_returns_are_rejected() -> None:
    """少给一条就等于少一个向量，写库时会导致切片与向量错位。"""

    class _DroppingEmbeddings(_RecordingEmbeddings):
        async def aembed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
            self.batch_sizes.append(len(texts))
            return [self._vector(texts[0])]

    provider = LangChainEmbedding(embeddings=_DroppingEmbeddings(), batch_size=4)

    with pytest.raises(AppError) as excinfo:
        asyncio.run(provider.embed_texts(["a", "b"]))

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED


def test_non_positive_batch_size_is_rejected() -> None:
    """与旧直连实现同一个入参契约：batch_size 必须 >= 1。"""

    with pytest.raises(ValueError):
        LangChainEmbedding(embeddings=_RecordingEmbeddings(), batch_size=0)
