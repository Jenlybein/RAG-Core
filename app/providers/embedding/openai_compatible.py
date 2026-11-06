"""OpenAI 兼容的 Embedding 实现（百炼走这个协议）。

请求必须带 dimensions=1536。百炼单批上限按模型不同：text-embedding-v3/v4 = 10、
v1/v2 = 25、qwen3.7 = 20；批次大小由 settings.EMBEDDING_BATCH_SIZE 传入，默认 10。
骨架期不做重试（阶段 8 再加），失败直接抛。
"""

import httpx

from app.providers.embedding.base import EmbeddingProvider


class OpenAICompatibleEmbedding(EmbeddingProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        batch_size: int = 10,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._url = base_url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size

    async def _post(self, inputs: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": inputs, "dimensions": self._dimensions}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self._url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()["data"]
        # OpenAI 兼容协议不保证返回顺序，按 index 排回去
        return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            out.extend(await self._post(texts[i : i + self._batch_size]))
        return out

    async def embed_query(self, query: str) -> list[float]:
        return (await self._post([query]))[0]
