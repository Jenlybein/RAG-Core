"""LangChain 版 Embedding Provider：包住框架的 ``Embeddings`` 抽象。

**为什么分批写在这里，而不是交给框架的 ``chunk_size``**：批次大小是配置项
（``EMBEDDING_BATCH_SIZE``，按百炼各模型的单批上限给）。分批点固定，才能证明
「同一批 10 条文本，新实现分批调用与旧直连实现逐条调用得到同一组向量」——
这是本票的嵌入等价性验收；分批位置一变，比对就没有意义了。

**为什么关掉 ``check_embedding_ctx_length``**：开着时框架会先用 tiktoken 把文本切成
token 再拼批，同一段文本在两套实现里会被送成不同的输入（甚至截断），等价性无从谈起。
关掉后请求体与旧直连实现逐字段一致：model / input / dimensions。

**为什么错统一映射成 ``EMBEDDING_FAILED``**：索引任务按这个码判定可重试
（见 app/tasks/indexing.py），把 openai / httpx 的原始异常冒出去就丢了重试，
最终表现成「某篇文档永远卡在索引中」。

**模型对象每次现装，连接池不是**（第 10 步票 4）：Provider 对象是进程级缓存的，而框架
的 ``OpenAIEmbeddings`` 在构造时就把连接池焊在某个事件循环上。缓存它 = 把第一个 loop
的连接池交给第二个 loop（混合检索的向量分支有自己的 loop、Celery 每个任务一个），代价是
``RuntimeError: Event loop is closed``。所以 Provider 只持有**装配动作**，每次调用向
``app.core.http_clients`` 要「当前 loop 的客户端」再装一层薄包装（实测 <1 ms）。
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.embeddings import Embeddings

from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding.base import EmbeddingProvider


class LangChainEmbedding(EmbeddingProvider):
    """分批 + 校验，真正的 HTTP 交给框架的 ``Embeddings`` 抽象。

    ``embeddings`` 与 ``embeddings_factory`` 二选一：前者是调用方自己造好的对象
    （测试替身、脚本体），连接归调用方；后者是生产路径——每次调用现装一个，客户端来自
    连接注册表（按当前事件循环）。
    """

    def __init__(
        self,
        *,
        embeddings: Embeddings | None = None,
        embeddings_factory: Callable[[], Embeddings] | None = None,
        batch_size: int = 10,
        dimensions: int | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if embeddings is None and embeddings_factory is None:
            raise ValueError("LangChainEmbedding 需要 embeddings 或 embeddings_factory 之一")
        self._embeddings = embeddings
        self._embeddings_factory = embeddings_factory
        self._batch_size = batch_size
        self._dimensions = dimensions

    def _model(self) -> Embeddings:
        """这一次调用该用的框架实现：注入的那个原样返回，否则现装一个。

        「现装」不含任何网络动作，连接池来自注册表——所以同一个 loop 里的连续调用命中
        同一个池子，而模型对象本身不进任何进程级缓存（那会把 loop 焊进缓存里）。
        """

        if self._embeddings is not None:
            return self._embeddings
        factory = self._embeddings_factory
        if factory is None:  # pragma: no cover - 构造期已经拦住
            raise RuntimeError("LangChainEmbedding 没有可用的装配方式")
        return factory()

    @property
    def embeddings(self) -> Embeddings:
        """按当前 loop 现装一个底层实现（只读、不发请求）。

        给排障与测试用；每次访问都装一个新的——它没有连接状态，别拿它当单例。
        """

        return self._model()

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        embeddings = self._model()
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            try:
                batch_vectors = await embeddings.aembed_documents(batch)
            except Exception as exc:  # noqa: BLE001 - 上游实现各式各样，统一成一个业务码
                raise AppError(ErrorCode.EMBEDDING_FAILED, f"嵌入调用失败：{exc}") from exc
            self._check(batch_vectors, expected=len(batch))
            vectors.extend(batch_vectors)
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        try:
            vector = await self._model().aembed_query(query)
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrorCode.EMBEDDING_FAILED, f"嵌入调用失败：{exc}") from exc
        self._check([vector], expected=1)
        return vector

    def _check(self, vectors: list[list[float]], *, expected: int) -> None:
        """条数与维度都要对得上。

        少一条 = 切片与向量错位；维度变了 = 服务端换了模型或漏传 dimensions，
        写进库的向量与查询向量算不出有意义的内积，而这两种错误都不会自己报出来。
        """

        if len(vectors) != expected:
            raise AppError(
                ErrorCode.EMBEDDING_FAILED,
                f"嵌入返回条数不符：期望 {expected}，实际 {len(vectors)}",
            )
        if self._dimensions is None:
            return
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise AppError(
                    ErrorCode.EMBEDDING_FAILED,
                    f"嵌入维度不符：期望 {self._dimensions}，实际 {len(vector)}",
                )
