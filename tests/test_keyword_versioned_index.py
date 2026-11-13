"""#142 · 关键词索引按版本**物理隔离**，别名指向 active。

```text
rag_chunks          ← 别名：检索永远只认它
rag_chunks__v1      ← 物理索引（生效中的那一版）
rag_chunks__v2      ← 构建中的新版本，激活前无人指向它
```

**接缝**（父 spec 的 D 票不加新缝）：Provider 的公开方法 + 三个纯函数（命名 / 别名动作）。
执行层用注入的假客户端记录「到底把什么发了出去」——被断言的是我们自己造出来的请求形状，
不是 ES 客户端的实现细节。不连 ES、不发网络。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from elasticsearch import NotFoundError

from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.providers.keyword_search.versioned_index import (
    build_alias_actions,
    physical_index_name,
    version_from_physical_index,
    version_index_pattern,
)

ALIAS = "rag_chunks"
TENANT_ID = "tenant_demo"
KB_ID = "kb-a"


def _not_found() -> NotFoundError:
    """一个「索引不存在」的异常实例：只为让 Provider 走进它的 NotFound 分支。"""

    return NotFoundError.__new__(NotFoundError)


# ─────────────────────────────────────────────────────── 命名：纯函数


def test_physical_index_name_is_derived_from_the_alias_and_the_version() -> None:
    """``(别名, 版本)`` → 物理索引名；反解也要能回得来（排障时靠它认索引）。"""

    index = physical_index_name(ALIAS, "v2")

    assert index == "rag_chunks__v2"
    assert version_from_physical_index(ALIAS, index) == "v2"
    assert version_from_physical_index(ALIAS, "rag_chunks_v2") is None, "分隔符不对就不是我们的索引"
    assert version_from_physical_index(ALIAS, "rag_chunks") is None, "别名本身不是物理索引"


def test_the_version_pattern_covers_every_version_and_nothing_else() -> None:
    """``<别名>__*`` 覆盖全部版本，但**不**误伤 ``rag_chunks_eval`` 这种评测索引。"""

    pattern = version_index_pattern(ALIAS)

    assert pattern == "rag_chunks__*"
    assert physical_index_name(ALIAS, "v10").startswith("rag_chunks__")
    assert not "rag_chunks_eval".startswith("rag_chunks__")


def test_alias_actions_swap_in_one_call() -> None:
    """摘旧 + 挂新必须在**一次** ``update_aliases`` 里：分两次会留下「别名指向空」的窗口。"""

    actions = build_alias_actions(
        alias=ALIAS,
        target="rag_chunks__v2",
        current=["rag_chunks__v1"],
    )

    assert actions == [
        {"remove": {"index": "rag_chunks__v1", "alias": ALIAS}},
        {"add": {"index": "rag_chunks__v2", "alias": ALIAS}},
    ]


def test_activating_the_same_version_again_is_a_no_op() -> None:
    """重复激活不该再发一次可能失败的写（幂等是切换脚本敢重跑的前提）。"""

    assert (
        build_alias_actions(alias=ALIAS, target="rag_chunks__v2", current=["rag_chunks__v2"]) == []
    )


def test_the_first_activation_only_adds_the_alias() -> None:
    """全新部署还没有别名：第一次激活只有 ``add``。"""

    assert build_alias_actions(alias=ALIAS, target="rag_chunks__v1", current=[]) == [
        {"add": {"index": "rag_chunks__v1", "alias": ALIAS}}
    ]


def test_an_alias_pointing_at_two_indices_is_reduced_to_one() -> None:
    """别名被手工挂到两张索引上（事故现场）：激活要把它收回成一张。"""

    actions = build_alias_actions(
        alias=ALIAS,
        target="rag_chunks__v2",
        current=["rag_chunks__v1", "rag_chunks__v3", "rag_chunks__v2"],
    )

    assert actions == [
        {"remove": {"index": "rag_chunks__v1", "alias": ALIAS}},
        {"remove": {"index": "rag_chunks__v3", "alias": ALIAS}},
    ]


# ──────────────────────────────────────────────── 执行层：写入按版本分组


class _FakeIndices:
    def __init__(self) -> None:
        self.exists_result = True
        self.exists_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.alias_targets: dict[str, Any] = {}
        self.update_calls: list[dict] = []

    async def exists(self, **kwargs: Any) -> bool:
        self.exists_calls.append(kwargs)
        return self.exists_result

    async def create(self, **kwargs: Any) -> None:
        self.create_calls.append(kwargs)

    async def get_alias(self, **kwargs: Any) -> dict:
        if self.alias_targets.get("raise") is True:
            raise _not_found()
        return self.alias_targets

    async def update_aliases(self, **kwargs: Any) -> None:
        self.update_calls.append(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.indices = _FakeIndices()
        self.bulk_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.count_calls: list[dict] = []
        self.count_response: dict = {"count": 0}

    async def bulk(self, **kwargs: Any) -> dict:
        self.bulk_calls.append(kwargs)
        return {"errors": False}

    async def search(self, **kwargs: Any) -> dict:
        self.search_calls.append(kwargs)
        return {"hits": {"hits": []}}

    async def count(self, **kwargs: Any) -> dict:
        self.count_calls.append(kwargs)
        return self.count_response


def _provider(client: _FakeClient) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://es.example:9200", index_name=ALIAS, client=client
    )


def _chunk(chunk_id: str, *, version: str) -> dict:
    return {
        "id": chunk_id,
        "tenant_id": TENANT_ID,
        "kb_id": KB_ID,
        "document_id": "doc-1",
        "content": "切片原文",
        "retrieval_text": "文档：售后手册 正文：切片原文",
        "index_version": version,
        "meta": {"title": "售后手册"},
    }


def test_writes_go_into_the_physical_index_of_each_version() -> None:
    """两个版本一起写 → 两次 bulk、落到两张物理索引；绝不写进别名（那会串版本）。"""

    client = _FakeClient()

    asyncio.run(
        _provider(client).add_chunks(
            [_chunk("chunk-v1", version="v1"), _chunk("chunk-v2", version="v2")]
        )
    )

    assert [call["operations"][0] for call in client.bulk_calls] == [
        {"index": {"_index": "rag_chunks__v1", "_id": "chunk-v1"}},
        {"index": {"_index": "rag_chunks__v2", "_id": "chunk-v2"}},
    ]
    assert [call["index"] for call in client.indices.exists_calls] == [
        "rag_chunks__v1",
        "rag_chunks__v2",
    ]


def test_search_always_queries_the_alias_not_a_physical_index() -> None:
    """检索只认别名：它是「当前生效版本」的唯一入口，也是切换不需要改代码的原因。"""

    client = _FakeClient()

    asyncio.run(
        _provider(client).keyword_search(
            "退款", tenant_id=TENANT_ID, index_versions={KB_ID: "v1"}, top_k=3
        )
    )

    assert client.search_calls[0]["index"] == ALIAS


# ─────────────────────────────────────────────────── 激活：别名切换


def test_activation_switches_the_alias_to_the_new_physical_index() -> None:
    """一次 ``update_aliases``：摘旧、挂新。切换之前检索查的仍是旧索引。"""

    client = _FakeClient()
    # 目标物理索引还不存在（第一次上线 v2）：激活要先把它建出来。
    client.indices.exists_result = False
    client.indices.alias_targets = {"rag_chunks__v1": {"aliases": {ALIAS: {}}}}

    asyncio.run(_provider(client).activate_version("v2"))

    assert client.indices.update_calls == [
        {
            "actions": [
                {"remove": {"index": "rag_chunks__v1", "alias": ALIAS}},
                {"add": {"index": "rag_chunks__v2", "alias": ALIAS}},
            ]
        }
    ], "一次调用里完成切换"
    assert client.indices.create_calls[0]["index"] == "rag_chunks__v2", (
        "目标物理索引不存在就先建：指向不存在索引的别名会让关键词召回永远空"
    )
    assert client.search_calls == [], "激活不触发检索"


def test_activation_without_an_existing_alias_only_adds() -> None:
    """第一次激活（还没有别名）只有 ``add``。"""

    client = _FakeClient()
    client.indices.alias_targets = {}

    asyncio.run(_provider(client).activate_version("v1"))

    assert client.indices.update_calls == [
        {"actions": [{"add": {"index": "rag_chunks__v1", "alias": ALIAS}}]}
    ]


def test_activation_is_idempotent() -> None:
    """已经指向目标版本：不再发一次写（切换脚本因此可以重跑）。"""

    client = _FakeClient()
    client.indices.alias_targets = {"rag_chunks__v2": {"aliases": {ALIAS: {}}}}

    asyncio.run(_provider(client).activate_version("v2"))

    assert client.indices.update_calls == []


# ───────────────────────────────────────────── 计数：数的是目标版本那张索引


def test_count_documents_counts_the_physical_index_of_that_version() -> None:
    """四项计数之一：数目标版本物理索引里这个租户 / 库 / 版本的文档。"""

    client = _FakeClient()
    client.count_response = {"count": 42}

    count = asyncio.run(
        _provider(client).count_documents(tenant_id=TENANT_ID, kb_id=KB_ID, index_version="v2")
    )

    assert count == 42
    sent = client.count_calls[0]
    assert sent["index"] == "rag_chunks__v2", "数错索引 = 让四项计数这道闸门形同虚设"
    filters = sent["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": TENANT_ID}} in filters
    assert {"term": {"kb_id": KB_ID}} in filters
    assert {"term": {"index_version": "v2"}} in filters, "写错物理索引的文档会在这里露出来"


def test_count_documents_treats_a_missing_index_as_zero() -> None:
    """索引还不存在 = 这个版本一个文档都没有。0 是诚实数字，不是故障。"""

    client = _FakeClient()

    async def _missing(**_kwargs: Any) -> dict:
        raise _not_found()

    client.count = _missing  # type: ignore[method-assign]

    assert (
        asyncio.run(
            _provider(client).count_documents(tenant_id=TENANT_ID, kb_id=KB_ID, index_version="v2")
        )
        == 0
    )


def test_the_noop_provider_keeps_the_three_new_methods_quiet() -> None:
    """没有关键词引擎时：计数 0、没有别名可切、删除安静——构建编排据此跳过这一维。"""

    provider = NoopKeywordSearchProvider()

    assert (
        asyncio.run(provider.count_documents(tenant_id=TENANT_ID, kb_id=KB_ID, index_version="v2"))
        == 0
    )
    assert asyncio.run(provider.activate_version("v2")) is None
    assert (
        asyncio.run(
            provider.delete_by_document_id("doc-1", tenant_id=TENANT_ID, index_version="v2")
        )
        is None
    )


@pytest.mark.parametrize("version", ["v1", "v2", "v10"])
def test_physical_names_are_unique_per_version(version: str) -> None:
    """每版一张物理索引：同版本同名字（幂等），不同版本不同名字（隔离）。"""

    assert physical_index_name(ALIAS, version) != physical_index_name(ALIAS, "other")
    assert physical_index_name(ALIAS, version) == physical_index_name(ALIAS, version)
