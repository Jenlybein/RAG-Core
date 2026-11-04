"""OpenAI 兼容的 Embedding 实现（百炼走这个协议）。

请求必须带 dimensions=1536。百炼单批上限 20 条，这里取 10 留重试余量。
骨架期不做重试（阶段 8 再加），失败直接抛。
"""

import httpx

from app.providers.embedding.base import EmbeddingProvider

BATCH_SIZE = 10


class OpenAICompatibleEmbedding(EmbeddingProvider):
    def __init__(self, *, base_url: str, api_key: str, model: str, dimensions: int) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions

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
        for i in range(0, len(texts), BATCH_SIZE):
            out.extend(await self._post(texts[i : i + BATCH_SIZE]))
        return out

    async def embed_query(self, query: str) -> list[float]:
        return (await self._post([query]))[0]