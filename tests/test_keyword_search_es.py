"""真实 Elasticsearch 上的关键词通道冒烟（``-m integration`` 才跑）。

只验证三件事：客户端版本与服务端对得上、查询体能真的执行、中文确实被切成词。
**不注入任何传输层 mock**：那测的是 ES 客户端的实现细节，升级就碎。

默认离线套件不碰它：``RAG_INTEGRATION_ELASTICSEARCH_URL`` 未设置时整模块 skip
（与 ``tests/test_integration_cas.py`` 用环境变量守真实依赖的做法一致）。

跑法::

    docker compose -f deploy/docker-compose.yml up -d elasticsearch
    $env:RAG_INTEGRATION_ELASTICSEARCH_URL="http://localhost:9200"
    uv run pytest -m integration tests/test_keyword_search_es.py -q

用例自建一个带随机后缀的临时索引，结束时删掉，绝不碰 ``rag_chunks``。

**一个事件循环跑完整条用例**：``AsyncElasticsearch`` 的 aiohttp 会话与首个使用
它的循环绑定，在同一个测试里反复 ``asyncio.run`` 会在第二次调用时炸
「Event loop is closed」，所以每个用例都只有一个 ``asyncio.run``。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from elasticsearch import AsyncElasticsearch

from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider

ELASTICSEARCH_URL = os.environ.get("RAG_INTEGRATION_ELASTICSEARCH_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not ELASTICSEARCH_URL,
        reason="RAG_INTEGRATION_ELASTICSEARCH_URL is not set; integration tests skipped",
    ),
]

TENANT_ID = "tenant_it"
OTHER_TENANT_ID = "tenant_other"
KB_ID = "kb-it"
DOC_A = "doc-it-a"
DOC_B = "doc-it-b"
INDEX_VERSION = "v1"


def _url() -> str:
    assert ELASTICSEARCH_URL is not None
    return ELASTICSEARCH_URL


def _temporary_index() -> str:
    """临时索引：随机后缀避免与开发环境的 ``rag_chunks`` 撞车。"""

    return f"rag_chunks_it_{uuid.uuid4().hex[:8]}"


def _chunk(chunk_id: str, document_id: str, text: str, retrieval_text: str) -> dict:
    return {
        "id": chunk_id,
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "document_id": document_id,
        "content": text,
        "retrieval_text": retrieval_text,
        "index_version": INDEX_VERSION,
        "meta": {"title": "售后手册"},
    }


CHUNKS = [
    _chunk(
        "chunk-a1",
        DOC_A,
        "错误码 E1024 表示证书过期。",
        "文档：售后手册 章节：错误码 正文：错误码 E1024 表示证书过期。",
    ),
    _chunk(
        "chunk-a2",
        DOC_A,
        "退款申请流程：提交工单后 3 个工作日内到账。",
        "文档：售后手册 章节：退款 > 流程 正文：退款申请流程：提交工单后 3 个工作日内到账。",
    ),
    _chunk(
        "chunk-b1",
        DOC_B,
        "型号 XR-2000 的保修期为两年。",
        "文档：售后手册 章节：保修 正文：型号 XR-2000 的保修期为两年。",
    ),
]


def test_ik_analyzer_is_installed_and_splits_chinese() -> None:
    """IK 插件必须真的在：写入用细粒度分词，「退款申请流程」要切成多个词。"""

    asyncio.run(_check_ik_analyzer(_url()))


async def _check_ik_analyzer(url: str) -> None:
    client = AsyncElasticsearch(url)
    try:
        response = await client.indices.analyze(analyzer="ik_max_word", text="退款申请流程")
    finally:
        await client.close()

    tokens = [token["token"] for token in response["tokens"]]
    assert len(tokens) > 1, f"整句没有被切开，IK 可能没装：{tokens}"
    assert "".join(tokens) == "退款申请流程"


def test_keyword_channel_end_to_end() -> None:
    """建索引 → 写 3 条 → 中文/字面命中 → 按文档删除 → 计数归零。"""

    asyncio.run(_end_to_end(_url(), _temporary_index()))


async def _end_to_end(url: str, index_name: str) -> None:
    client = AsyncElasticsearch(url)
    provider = ElasticsearchKeywordSearchProvider(url=url, index_name=index_name, client=client)
    try:
        await provider.add_chunks(CHUNKS)
        await client.indices.refresh(index=index_name)

        # 1) 三条切片全部落在临时索引里（双写里「关键词一侧」的计数）。
        total = await client.count(index=index_name)
        assert total["count"] == 3

        # 2) 字段级分词器：写入细粒度、查询智能模式（mapping 显式声明过）。
        analyzed = await client.indices.analyze(
            index=index_name, field="retrieval_text", text="退款申请流程"
        )
        field_tokens = [token["token"] for token in analyzed["tokens"]]
        assert len(field_tokens) > 1

        # 3) 精确字面命中：E1024。
        literal_hits = await provider.keyword_search(
            "E1024", tenant_id=TENANT_ID, index_versions={KB_ID: INDEX_VERSION}, top_k=5
        )
        assert [hit["chunk_id"] for hit in literal_hits][:1] == ["chunk-a1"]
        assert literal_hits[0]["content"] == "错误码 E1024 表示证书过期。", "返回的是切片原文"

        # 4) 中文词命中：「退款」不是整句，也不是单字。
        chinese_hits = await provider.keyword_search(
            "退款", tenant_id=TENANT_ID, index_versions={KB_ID: INDEX_VERSION}, top_k=5
        )
        assert "chunk-a2" in [hit["chunk_id"] for hit in chinese_hits]

        # 5) 租户过滤：别的租户一条都看不到。
        other_tenant = await provider.keyword_search(
            "E1024", tenant_id=OTHER_TENANT_ID, index_versions={KB_ID: INDEX_VERSION}, top_k=5
        )
        assert other_tenant == []

        # 6) 库 + 版本配对：版本对不上就召回不到。
        wrong_version = await provider.keyword_search(
            "E1024", tenant_id=TENANT_ID, index_versions={KB_ID: "v2"}, top_k=5
        )
        assert wrong_version == []

        # 7) 按文档删除：只删 DOC_A 的两条，另一份文档还在。
        await provider.delete_by_document_id(DOC_A, tenant_id=TENANT_ID)
        await client.indices.refresh(index=index_name)
        remaining = await client.count(index=index_name)
        assert remaining["count"] == 1

        await provider.delete_by_document_id(DOC_B, tenant_id=TENANT_ID)
        await client.indices.refresh(index=index_name)
        assert (await client.count(index=index_name))["count"] == 0
    finally:
        await client.indices.delete(index=index_name, ignore_unavailable=True)
        await client.close()
