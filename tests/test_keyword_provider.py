"""关键词检索 Provider 的离线契约：空实现、工厂选择、执行层的组装。

默认离线套件不许碰网络，所以这里**不连真实 ES**：工厂只做选择与装配，
执行层用一个注入的假客户端记录「到底把什么发了出去」——被断言的是我们自己
造出来的请求体与结果映射，不是 ES 客户端的实现细节。真实引擎的冒烟在
``tests/test_keyword_search_es.py``（``-m integration`` 才跑）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.providers.keyword_search import (
    KeywordSearchProvider,
    NoopKeywordSearchProvider,
    get_keyword_search_provider,
)
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.keyword_search.query_builder import build_bm25_query, build_purge_query

TENANT_ID = "tenant_demo"
KB_ID = "kb-a"


# ────────────────────────────────────────────────────────────── 空实现


def test_noop_is_explicitly_unavailable() -> None:
    """空实现不是「查不到」，而是「这项能力不存在」——调用方据此做硬失败。"""

    provider = NoopKeywordSearchProvider()

    assert isinstance(provider, KeywordSearchProvider)
    assert provider.available is False


def test_noop_returns_empty_results_and_writes_nothing() -> None:
    """写 / 删 / 查三条路径都不抛：索引链路遍历存储列表时它必须安静。"""

    provider = NoopKeywordSearchProvider()

    assert (
        asyncio.run(
            provider.keyword_search(
                "E1024", tenant_id=TENANT_ID, index_versions={KB_ID: "v1"}, top_k=5
            )
        )
        == []
    )
    assert asyncio.run(provider.add_chunks([{"id": "chunk-1"}])) is None
    assert asyncio.run(provider.delete_by_document_id("doc-1", tenant_id=TENANT_ID)) is None


# ────────────────────────────────────────────────────────────── 工厂


def test_factory_defaults_to_the_noop_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有显式配置关键词引擎时返回空实现：离线环境照常启动。"""

    monkeypatch.setattr(settings, "KEYWORD_SEARCH_PROVIDER", "none")

    assert isinstance(get_keyword_search_provider(), NoopKeywordSearchProvider)


def test_factory_builds_the_elasticsearch_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``elasticsearch`` 装配 ES 实现，地址与索引名全部来自配置。"""

    monkeypatch.setattr(settings, "KEYWORD_SEARCH_PROVIDER", "elasticsearch")
    monkeypatch.setattr(settings, "ELASTICSEARCH_URL", "http://es.example:9200")
    monkeypatch.setattr(settings, "ELASTICSEARCH_INDEX", "rag_chunks_test")

    provider = get_keyword_search_provider()

    assert isinstance(provider, ElasticsearchKeywordSearchProvider)
    assert provider.available is True
    assert provider.index_name == "rag_chunks_test"


def test_factory_rejects_unknown_provider_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """拼错的名字必须当场炸，不能悄悄退回空实现（那会变成静默不检索）。"""

    monkeypatch.setattr(settings, "KEYWORD_SEARCH_PROVIDER", "elastic")

    with pytest.raises(ValueError, match="KEYWORD_SEARCH_PROVIDER"):
        get_keyword_search_provider()


def test_constructing_the_es_provider_opens_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造只存参数；连不上也不该在装配阶段失败（服务要能启动）。"""

    monkeypatch.setattr(settings, "KEYWORD_SEARCH_PROVIDER", "elasticsearch")
    monkeypatch.setattr(settings, "ELASTICSEARCH_URL", "http://127.0.0.1:1")

    provider = get_keyword_search_provider()

    assert provider.available is True
    assert provider.index_name == settings.ELASTICSEARCH_INDEX


# ──────────────────────────────────────────── 执行层：把造好的请求体发出去


class _FakeIndices:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists_result = exists
        self.exists_calls: list[dict] = []
        self.create_calls: list[dict] = []

    async def exists(self, **kwargs):
        self.exists_calls.append(kwargs)
        return self.exists_result

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)


class _FakeClient:
    """记录调用参数、返回固定响应的假客户端（不实现传输层）。"""

    def __init__(self) -> None:
        self.indices = _FakeIndices()
        self.search_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.bulk_calls: list[dict] = []
        self.response: dict = {"hits": {"hits": []}}

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.response

    async def delete_by_query(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {"deleted": 0}

    async def bulk(self, **kwargs):
        self.bulk_calls.append(kwargs)
        return {"errors": False}


def _provider(client: _FakeClient) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://es.example:9200", index_name="rag_chunks", client=client
    )


def test_keyword_search_sends_the_pure_query_and_maps_the_hits() -> None:
    """请求体必须与纯函数一字不差；命中映射成业务契约里的键，不暴露 ES 字段名。"""

    client = _FakeClient()
    client.response = {
        "hits": {
            "hits": [
                {
                    "_score": 12.5,
                    "_source": {
                        "tenant_id": TENANT_ID,
                        "kb_id": KB_ID,
                        "index_version": "v1",
                        "document_id": "doc-1",
                        "chunk_id": "chunk-1",
                        "title": "售后手册",
                        "content": "退款申请流程",
                    },
                }
            ]
        }
    }

    rows = asyncio.run(
        _provider(client).keyword_search(
            "退款申请流程",
            tenant_id=TENANT_ID,
            index_versions={KB_ID: "v1"},
            top_k=3,
        )
    )

    sent = client.search_calls[0]
    assert sent["index"] == "rag_chunks"
    assert sent["size"] == 3
    assert (
        sent["query"]
        == build_bm25_query(
            tenant_id=TENANT_ID,
            index_versions={KB_ID: "v1"},
            search_query="退款申请流程",
            top_k=3,
        )["query"]
    )

    assert rows == [
        {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "kb_id": KB_ID,
            "title": "售后手册",
            "content": "退款申请流程",
            "score": 12.5,
        }
    ]


def test_delete_by_document_id_sends_the_purge_query() -> None:
    """按文档删除：查询体来自纯函数，并且要求刷新后才返回。"""

    client = _FakeClient()

    asyncio.run(_provider(client).delete_by_document_id("doc-1", tenant_id=TENANT_ID))

    sent = client.delete_calls[0]
    assert sent["index"] == "rag_chunks"
    assert sent["query"] == build_purge_query(document_id="doc-1", tenant_id=TENANT_ID)["query"]
    assert sent["refresh"] is True


def test_add_chunks_writes_the_retrieval_text_under_chunk_id() -> None:
    """写入用 chunk_id 当 ``_id``：重复索引同一份文档天然幂等。"""

    client = _FakeClient()
    chunk = {
        "id": "chunk-1",
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "document_id": "doc-1",
        "content": "切片原文",
        "retrieval_text": "文档：售后手册 正文：切片原文",
        "index_version": "v1",
        "meta": {"title": "售后手册"},
    }

    asyncio.run(_provider(client).add_chunks([chunk]))

    operations = client.bulk_calls[0]["operations"]
    assert operations[0] == {"index": {"_index": "rag_chunks", "_id": "chunk-1"}}
    assert operations[1]["retrieval_text"] == "文档：售后手册 正文：切片原文"
    assert operations[1]["content"] == "切片原文"
    assert client.bulk_calls[0]["refresh"] is True


def test_add_chunks_creates_the_index_when_missing() -> None:
    """写入前先幂等建索引；索引阶段不做降级，建不出来就抛。"""

    client = _FakeClient()
    client.indices.exists_result = False
    chunk = {
        "id": "chunk-1",
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "document_id": "doc-1",
        "content": "切片原文",
        "retrieval_text": "文档：售后手册 正文：切片原文",
        "index_version": "v1",
        "meta": {"title": "售后手册"},
    }

    asyncio.run(_provider(client).add_chunks([chunk]))

    assert client.indices.exists_calls == [{"index": "rag_chunks"}]
    assert client.indices.create_calls[0]["index"] == "rag_chunks"
    assert client.indices.create_calls[0]["mappings"]["properties"]["tenant_id"] == {
        "type": "keyword"
    }
    settings_payload = client.indices.create_calls[0]["settings"]
    assert settings_payload["number_of_shards"] == 1
    assert settings_payload["number_of_replicas"] == 0
    # 查询侧分析器随建索引一起注册（自定义分析器是索引级设置，事后补不了）。
    assert "ik_smart_query" in settings_payload["analysis"]["analyzer"]
