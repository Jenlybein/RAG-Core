"""关键词通道的三个纯函数：mapping / 查询体 / 按文档删除的查询体。

这三个函数是票 23 最值钱的可测部分：它们**不 import 数据库、不发网络请求**，
返回值就是发出去的 JSON 本身，所以断言直接对着字典写。执行层（ES 客户端）
只负责把这里造好的东西发出去，测试离线全套照跑。

被钉死的四件事（也是教程里最常写错的地方）：
1. 过滤维度一律 ``keyword``，不参与分词；
2. ``title`` 与检索文本用中文分词，且**显式声明查询分词器**
   （写入 ``ik_max_word`` 细粒度 / 查询 ``ik_smart`` 智能模式）；
3. 租户过滤走 ``filter``；「库 + 版本」是**配对条件**，每对之间是 ``should`` +
   ``minimum_should_match=1``，绝不用两个独立 ``IN``；
4. 关键词打分走 ``multi_match``，标题权重高于正文。
"""

from __future__ import annotations

import json

from app.providers.keyword_search.query_builder import (
    DEFAULT_INDEX_VERSION,
    build_bm25_query,
    build_es_mapping,
    build_keyword_documents,
    build_purge_query,
)

TENANT_ID = "tenant_demo"
KB_A = "kb-a"
KB_B = "kb-b"


# ─────────────────────────────────────────────────────────────── mapping 构造


def _properties() -> dict:
    mapping = build_es_mapping()
    return mapping["mappings"]["properties"]


def test_mapping_keeps_every_filter_field_as_keyword() -> None:
    """租户 / 库 / 版本 / 文档 / 切片是过滤维度：keyword，不做分词。"""

    properties = _properties()

    for field in ("tenant_id", "kb_id", "index_version", "document_id", "chunk_id"):
        assert properties[field] == {"type": "keyword"}, f"{field} 必须是 keyword"


def test_mapping_declares_chinese_analyzers_for_title_and_retrieval_text() -> None:
    """写入细粒度、查询智能模式：两个分词器都要显式写出来。

    只写 ``analyzer`` 不写 ``search_analyzer`` 时，查询也会走 ``ik_max_word``，
    一个问句会被切成一把碎词，召回率反而掉下来——这是教程原文写错的那一处。
    """

    properties = _properties()

    assert properties["title"]["analyzer"] == "ik_max_word"
    assert properties["title"]["search_analyzer"] == "ik_smart"
    assert properties["retrieval_text"]["analyzer"] == "ik_max_word"
    assert properties["retrieval_text"]["search_analyzer"] == "ik_smart"
    assert properties["title"]["type"] == properties["retrieval_text"]["type"] == "text"


def test_mapping_stores_the_original_content_without_indexing_it() -> None:
    """切片原文只存不索引：命中靠检索文本，返回给调用方的仍是原文。"""

    properties = _properties()

    assert properties["content"]["index"] is False
    assert "analyzer" not in properties["content"], "不索引的字段不该声明分词器"


# ────────────────────────────────────────────────────────── build_bm25_query


def _bm25(**overrides) -> dict:
    payload = {
        "tenant_id": TENANT_ID,
        "index_versions": {KB_A: "v1", KB_B: "v1"},
        "search_query": "退款申请流程",
        "top_k": 5,
    }
    payload.update(overrides)
    return build_bm25_query(**payload)


def test_bm25_query_filters_by_tenant_inside_filter() -> None:
    """租户过滤是 ``filter`` 里的 term：不算分、能走倒排缓存，也不影响打分。"""

    query = _bm25()
    bool_query = query["query"]["bool"]

    assert {"term": {"tenant_id": TENANT_ID}} in bool_query["filter"]
    assert "tenant_id" not in json.dumps(bool_query.get("must", [])), "租户不进 must"


def test_bm25_query_pairs_kb_and_index_version_with_should() -> None:
    """「库 + 版本」必须是配对条件：每对之间是 should，msm=1。

    写成两个独立的 ``IN``（kb_id IN (...) AND index_version IN (...)）会匹配出
    「A 库配 B 库的版本」这种不存在的组合，越权与脏数据都从这里来。
    """

    query = _bm25()
    bool_query = query["query"]["bool"]
    pairing = bool_query["filter"][1]

    assert pairing["bool"]["minimum_should_match"] == 1
    should = pairing["bool"]["should"]
    assert len(should) == 2, "一个库一条配对条件，不是两个独立 IN"
    assert {
        "bool": {"filter": [{"term": {"kb_id": KB_A}}, {"term": {"index_version": "v1"}}]}
    } in should
    assert {
        "bool": {"filter": [{"term": {"kb_id": KB_B}}, {"term": {"index_version": "v1"}}]}
    } in should

    # 任何形式的 terms 查询都会退化成「两个独立 IN」，这里一次都不许出现。
    assert '"terms"' not in json.dumps(query)


def test_bm25_query_uses_different_versions_per_kb() -> None:
    """同一对必须是「这个库 + 这个版本」；两个库可以有不同版本。"""

    query = _bm25(index_versions={KB_A: "v1", KB_B: "v2"})
    should = query["query"]["bool"]["filter"][1]["bool"]["should"]

    pairs = [
        (
            clause["bool"]["filter"][0]["term"]["kb_id"],
            clause["bool"]["filter"][1]["term"]["index_version"],
        )
        for clause in should
    ]
    assert pairs == [(KB_A, "v1"), (KB_B, "v2")]


def test_bm25_query_scores_with_multi_match_and_title_weight() -> None:
    """打分走 multi_match：title^2 的权重严格高于检索文本。"""

    must = _bm25(search_query="E1024")["query"]["bool"]["must"]

    assert len(must) == 1
    multi_match = must[0]["multi_match"]
    assert multi_match["query"] == "E1024"
    assert multi_match["fields"] == ["title^2", "retrieval_text"]
    assert multi_match["fields"][0] == "title^2"


def test_bm25_query_carries_size_and_the_source_fields() -> None:
    """``size`` 就是 top_k；``_source`` 只取组装响应要用的字段。"""

    query = _bm25(top_k=3)

    assert query["size"] == 3
    assert set(query["_source"]) == {
        "tenant_id",
        "kb_id",
        "index_version",
        "document_id",
        "chunk_id",
        "title",
        "content",
    }


def test_bm25_query_with_no_scope_matches_nothing_by_construction() -> None:
    """一个库都没传时 msm=1 + 空 should = 匹配不到任何文档，绝不退化成全量。"""

    query = _bm25(index_versions={})
    pairing = query["query"]["bool"]["filter"][1]["bool"]

    assert pairing["should"] == []
    assert pairing["minimum_should_match"] == 1


# ───────────────────────────────────────────────────────── build_purge_query


def test_purge_query_deletes_by_document_and_tenant_only() -> None:
    """按文档清理必须同时带租户：否则会删掉别的租户同 id 的行。"""

    query = build_purge_query(document_id="doc-1", tenant_id=TENANT_ID)

    assert query == {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tenant_id": TENANT_ID}},
                    {"term": {"document_id": "doc-1"}},
                ]
            }
        }
    }


# ──────────────────────────────────────────────────── 切片 → 关键词文档


def _chunk(**overrides) -> dict:
    chunk = {
        "id": "chunk-1",
        "tenant_id": TENANT_ID,
        "kb_id": KB_A,
        "document_id": "doc-1",
        "content": "退款申请流程：提交工单后 3 个工作日内到账。",
        "retrieval_text": "文档：售后手册 章节：退款 > 流程 正文：退款申请流程：提交工单后 3 个工作日内到账。",
        "embedding": [0.1, 0.2],
        "index_version": "v1",
        "meta": {"title": "售后手册", "heading_path": "退款 > 流程"},
    }
    chunk.update(overrides)
    return chunk


def test_keyword_documents_index_the_retrieval_text_not_the_raw_content() -> None:
    """两路必须吃同一份文本：关键词文档的可检索字段就是检索文本。"""

    document = build_keyword_documents([_chunk()])[0]

    assert document["retrieval_text"].startswith("文档：售后手册 章节：退款 > 流程 正文：")
    assert document["retrieval_text"] != document["content"]


def test_keyword_documents_keep_the_raw_content_for_the_response() -> None:
    """返回给调用方的仍是切片原文：ES 只多存一份副本，不替换。"""

    document = build_keyword_documents([_chunk()])[0]

    assert document["content"] == "退款申请流程：提交工单后 3 个工作日内到账。"


def test_keyword_documents_carry_every_scope_field() -> None:
    """与 mapping 一一对应：租户 / 库 / 版本 / 文档 / 切片 / 标题一个都不能少。"""

    document = build_keyword_documents([_chunk()])[0]

    assert document["tenant_id"] == TENANT_ID
    assert document["kb_id"] == KB_A
    assert document["index_version"] == "v1"
    assert document["document_id"] == "doc-1"
    assert document["chunk_id"] == "chunk-1"
    assert document["title"] == "售后手册"


def test_keyword_documents_fall_back_to_the_default_index_version() -> None:
    """调用方没给版本时用列默认值相同的常量，绝不写空串。"""

    chunk = _chunk()
    chunk.pop("index_version")

    document = build_keyword_documents([chunk])[0]

    assert document["index_version"] == DEFAULT_INDEX_VERSION == "v1"
