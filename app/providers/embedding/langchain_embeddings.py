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
"""

from __future__ import annotations

from langchain_core.embeddings import Embeddings

from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding.base import EmbeddingProvider


class LangChainEmbedding(EmbeddingProvider):
    def __init__(
        self,
        *,
        embeddings: Embeddings,
        batch_size: int = 10,
        dimensions: int | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._embeddings = embeddings
        self._batch_size = batch_size
        self._dimensions = dimensions

    @property
    def embeddings(self) -> Embeddings:
        """构造时的底层实现（只读，排障与测试用）。"""

        return self._embeddings

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            try:
                batch_vectors = await self._embeddings.aembed_documents(batch)
            except Exception as exc:  # noqa: BLE001 - 上游实现各式各样，统一成一个业务码
                raise AppError(ErrorCode.EMBEDDING_FAILED, f"嵌入调用失败：{exc}") from exc
            self._check(batch_vectors, expected=len(batch))
            vectors.extend(batch_vectors)
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        try:
            vector = await self._embeddings.aembed_query(query)
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
