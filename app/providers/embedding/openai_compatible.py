"""OpenAI 兼容的 Embedding 实现（百炼走这个协议）。

请求必须带 dimensions=1536。百炼单批上限按模型不同：text-embedding-v3/v4 = 10、
v1/v2 = 25、qwen3.7 = 20；批次大小由 settings.EMBEDDING_BATCH_SIZE 传入，默认 10。
骨架期不做重试（阶段 8 再加），失败直接抛。

**客户端来自连接注册表**（第 10 步票 4）：改造前每次 ``_post`` 都 ``async with
httpx.AsyncClient(...)``，也就是每条请求都重做一次 TLS 握手、每个客户端用完即弃。
现在向 ``app.core.http_clients`` 要「当前事件循环的客户端」，超时按用途（嵌入 60 秒）
**按请求**传——同一个池子要服务不同预算的调用，超时不能焊在客户端上。
"""

import httpx

from app.core import http_clients
from app.providers.embedding.base import EmbeddingProvider

#: 嵌入请求的时间预算。与旧直连实现一致：批量嵌入一篇长文档往往要几十秒。
REQUEST_TIMEOUT_SECONDS = 60.0


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
        resp: httpx.Response = await http_clients.get_http_client().post(
            self._url,
            headers=headers,
            json=payload,
            # 预算按用途传：注册表里的客户端是共用的，超时不能写死在它身上。
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
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
