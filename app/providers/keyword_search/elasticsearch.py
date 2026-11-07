"""Elasticsearch 关键词检索实现：只做「组装与调用」。

请求体、mapping、切片文档的形状全部由 ``query_builder`` 的三个纯函数产出，
这一层不拼过滤条件、不做分词选择，也不在这里降级：索引阶段任一次写入失败
就是整单失败（调用方回滚事务并把文档置为失败），检索阶段能不能退回向量由
业务层决定，不是 Provider 的权限。

``refresh=True`` 是刻意的：文档索引完必须立刻能查到，否则「上传完马上提问」
会稳定地查不到内容。批量导入的吞吐优化留给后续步骤。
"""

from __future__ import annotations

from typing import Any

from elasticsearch import AsyncElasticsearch, BadRequestError, NotFoundError

from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.query_builder import (
    build_bm25_query,
    build_es_mapping,
    build_keyword_documents,
    build_purge_query,
)


class ElasticsearchKeywordSearchProvider(KeywordSearchProvider):
    """ES 8.x 实现。客户端惰性构造：装配阶段绝不发网络请求。"""

    def __init__(self, *, url: str, index_name: str, client: Any | None = None) -> None:
        self._url = url
        self._index_name = index_name
        self._client = client
        self._index_ready = False

    @property
    def available(self) -> bool:
        return True

    @property
    def index_name(self) -> str:
        return self._index_name

    def _get_client(self) -> AsyncElasticsearch:
        if self._client is None:
            self._client = AsyncElasticsearch(self._url)
        return self._client

    async def ensure_index(self) -> None:
        """幂等建索引：已存在就跳过；并发建索引时把「已存在」当作成功。"""

        if self._index_ready:
            return
        client = self._get_client()
        if not await client.indices.exists(index=self._index_name):
            try:
                await client.indices.create(index=self._index_name, **build_es_mapping())
            except BadRequestError as exc:
                # 两个 Worker 同时第一次索引时，后手会拿到 400
                # 「resource_already_exists_exception」。那是幂等，不是失败；
                # 其他 400（mapping 写错等）必须照常抛出去。
                if exc.error != "resource_already_exists_exception":
                    raise
        self._index_ready = True

    async def add_chunks(self, chunks: list[dict]) -> None:
        documents = build_keyword_documents(chunks)
        if not documents:
            return

        await self.ensure_index()
        operations: list[dict] = []
        for document in documents:
            operations.append(
                {"index": {"_index": self._index_name, "_id": document["chunk_id"]}}
            )
            operations.append(document)

        response = await self._get_client().bulk(operations=operations, refresh=True)
        if response.get("errors"):
            # bulk 是「部分成功」的接口：不主动抛就会留下半边数据，索引阶段必须失败。
            raise RuntimeError("elasticsearch bulk indexing reported errors")

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: dict[str, str], top_k: int
    ) -> list[dict]:
        payload = build_bm25_query(
            tenant_id=tenant_id,
            index_versions=index_versions,
            search_query=query,
            top_k=top_k,
        )
        try:
            response = await self._get_client().search(index=self._index_name, **payload)
        except NotFoundError:
            # 索引还没建 = 关键词库里一个字都没有。这不是故障，是诚实的空结果。
            return []

        return [
            {
                "chunk_id": hit["_source"]["chunk_id"],
                "document_id": hit["_source"]["document_id"],
                "kb_id": hit["_source"]["kb_id"],
                "title": hit["_source"].get("title", ""),
                "content": hit["_source"].get("content", ""),
                "score": float(hit.get("_score") or 0.0),
            }
            for hit in response["hits"]["hits"]
        ]

    async def delete_by_document_id(self, document_id: str, *, tenant_id: str) -> None:
        try:
            await self._get_client().delete_by_query(
                index=self._index_name,
                **build_purge_query(document_id=document_id, tenant_id=tenant_id),
                refresh=True,
            )
        except NotFoundError:
            # 索引不存在 = 本来就没有可删的文档。
            return None
