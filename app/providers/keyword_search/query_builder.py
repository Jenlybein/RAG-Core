"""关键词索引的三个纯函数：mapping、查询体、按文档删除的查询体。

**为什么单独一模块**：执行层（``elasticsearch.py``）只做「组装 + 发出去」，
所有值得断言的东西都落在返回的字典上。这里不 import 数据库会话、不构造任何
客户端、不发网络请求——离线测试直接对着字典断言即可，升级 ES 客户端也不会
把测试一起打碎。

三处最容易被写错的地方，都在这里钉死：

1. **过滤维度用 keyword、可检索文本用中文分词**。租户 / 库 / 版本 / 文档 /
   切片只做精确匹配，分词只会让它们变慢且匹配不到。
2. **写入与查询用两个分词器**。写入 ``ik_max_word``（细粒度，尽量多建词），
   查询 ``ik_smart``（智能切分，少切碎）。只写 ``analyzer`` 不写
   ``search_analyzer`` 时查询也会走细粒度，问句被切碎、召回率反而下降。
3. **「库 + 版本」是配对条件**。每对之间是 ``should`` + ``minimum_should_match=1``；
   写成两个独立的 ``terms``（即两个 ``IN``）会匹配出「A 库配 B 库的版本」这种
   不存在的组合。
"""

from __future__ import annotations

from app.models.chunk import DEFAULT_INDEX_VERSION

# 可检索文本的字段名。切片原文另存 ``content``（只存不索引），用来原样回给调用方。
RETRIEVAL_TEXT_FIELD = "retrieval_text"

# 写入 / 查询分词器：写入细粒度、查询智能模式。
INDEX_ANALYZER = "ik_max_word"
SEARCH_ANALYZER = "ik_smart"

# 返回给调用方要用的字段（``_source`` 白名单），别把 embedding 之类的整条拖回来。
SOURCE_FIELDS = (
    "tenant_id",
    "kb_id",
    "index_version",
    "document_id",
    "chunk_id",
    "title",
    "content",
)


def build_es_mapping() -> dict:
    """返回建索引的请求体（``indices.create(index=..., **build_es_mapping())``）。

    单分片零副本是**单机开发环境**的默认值：ES 的默认副本数是 1，单节点集群
    永远到不了 green，健康检查会一直黄。
    """

    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                # 过滤维度：精确匹配，不参与分词。
                "tenant_id": {"type": "keyword"},
                "kb_id": {"type": "keyword"},
                "index_version": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                # 可检索文本：写入细粒度、查询智能模式，两个都要显式声明。
                "title": {
                    "type": "text",
                    "analyzer": INDEX_ANALYZER,
                    "search_analyzer": SEARCH_ANALYZER,
                },
                RETRIEVAL_TEXT_FIELD: {
                    "type": "text",
                    "analyzer": INDEX_ANALYZER,
                    "search_analyzer": SEARCH_ANALYZER,
                },
                # 切片原文：只存不索引，响应里给调用方的必须是原文而不是检索文本。
                "content": {"type": "text", "index": False},
            }
        },
    }


def build_bm25_query(
    *,
    tenant_id: str,
    index_versions: dict[str, str],
    search_query: str,
    top_k: int,
) -> dict:
    """返回 ``search`` 的请求体：租户过滤 + 库/版本配对 + multi_match 打分。

    ``index_versions`` 是「kb_id → 索引版本」的映射（与向量侧的范围版签名同形状），
    不是两个平行数组：配对关系只能从映射里读出来，数组一旦错位就会静默越权。
    """

    # 每对「库 + 版本」是一个独立的必须同时成立的子句；子句之间才是 should。
    pairing = [
        {
            "bool": {
                "filter": [
                    {"term": {"kb_id": kb_id}},
                    {"term": {"index_version": version}},
                ]
            }
        }
        for kb_id, version in index_versions.items()
    ]

    return {
        "size": top_k,
        "track_total_hits": False,
        "_source": list(SOURCE_FIELDS),
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": search_query,
                            # title^2：标题命中比正文命中更值钱（教程里的原始取舍）。
                            "fields": ["title^2", RETRIEVAL_TEXT_FIELD],
                            "type": "best_fields",
                        }
                    }
                ],
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    # 配对条件必须留在 filter 里：若把 should 放在顶层 bool，同时又
                    # 有 must，ES 的默认 minimum_should_match 会退化成 0，
                    # 于是「库 + 版本」这对条件变成了纯加分项——过滤形同虚设。
                    {"bool": {"should": pairing, "minimum_should_match": 1}},
                ],
            }
        },
    }


def build_purge_query(*, document_id: str, tenant_id: str) -> dict:
    """返回 ``delete_by_query`` 的请求体：只删这个租户的这一份文档。

    ``tenant_id`` 与 ``document_id`` 都是 ``term`` 且都在 ``filter`` 里：
    少一个就会删到别的租户同 id 的行（ADR-0006 的强制过滤同样适用于删除）。
    """

    return {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"document_id": document_id}},
                ]
            }
        }
    }


def build_keyword_documents(chunks: list[dict]) -> list[dict]:
    """把索引编排的切片字典转成 ES 文档（``_id`` 用 chunk_id，写入天然幂等）。

    关键口径：**可检索字段装检索文本**（``build_retrieval_text`` 的产物），
    与向量侧共用一份文本；切片原文另存 ``content``（mapping 里只存不索引），
    这样 ``mode=bm25`` 回给调用方的仍是原文，而不是「文档：… 正文：…」。
    """

    return [
        {
            "tenant_id": chunk["tenant_id"],
            "kb_id": chunk["kb_id"],
            # 索引版本是数据范围的一部分：缺省值与 chunks 列默认值同源。
            "index_version": chunk.get("index_version") or DEFAULT_INDEX_VERSION,
            "document_id": chunk["document_id"],
            "chunk_id": chunk["id"],
            "title": (chunk.get("meta") or {}).get("title", ""),
            RETRIEVAL_TEXT_FIELD: chunk["retrieval_text"],
            "content": chunk["content"],
        }
        for chunk in chunks
    ]
