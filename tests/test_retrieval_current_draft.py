"""#174 · 默认检索只看**当前稿**：五个口径一次对齐。

父 spec 把话说得很直：这条约束必须落在 SQL 与关键词索引的 ``filter`` 里，**不靠应用层
「记得过滤」**。于是本票要守的是同一个谓词在五处逐字相同：

1. 向量召回（``similarity_search`` / ``similarity_search_scope``）；
2. 关键词召回（``build_bm25_query`` 的 ``is_current`` term）；
3. 折叠取整节（``_unit_rows_statement`` / 父节正文）；
4. 空结果原因的计数（``_count_by_kb_ids_statement``）；
5. 溯源回查（``_meta_statement``：按切片 id 反查元数据 + 稿号）。

为什么值得为「一条 SQL 条件」写一整个文件：两稿切片并存的时刻（指针已指向 v2、v1 的
切片按保留策略还在）**只有靠过滤才不出现**，而漏掉任意一处都不报错——向量路会多召回
一份旧稿，折叠会把两稿拼成一条结果，计数会说「库里有内容」而召回一条都没有，溯源回查
会把旧稿的切片当成当前稿的。症状全是「结果看起来正常，只是多了一份/少了一份」。

接缝：与既有用例一致——纯函数编译成 SQL 断言（``test_vector_scope_search`` 的路子），
执行层用注入的假客户端 / 只记账的会话（``test_keyword_versioned_index`` 的路子）。
不上真库、不连 ES。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from app.models.chunk import DEFAULT_DOC_VERSION
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.providers.keyword_search.query_builder import (
    build_bm25_query,
    build_es_mapping,
    build_keyword_documents,
    build_mapping_update,
)
from app.providers.vectorstores.pgvector import PgVectorStore

TENANT = "tenant_demo"
KB = "kb-a"


def _sql(clause_or_statement: Any) -> str:
    """把表达式 / 语句编译成压平空白的 SQL，断言只对结构敏感、不对排版敏感。

    **不渲染字面量**：向量列是 ``Vector(1536)``，SQLAlchemy 没有它的字面量处理器
    （``test_vector_scope_search`` 里那条「真的会发出去的 SQL」也因此不用
    ``literal_binds``）。这里断言的是列与列的**比较**，本来就不需要值。
    """

    compiled = clause_or_statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split())


class _CapturingSession:
    """只接住语句、不执行的异步会话替身（不连库、不发 SQL）。"""

    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> SimpleNamespace:
        self.statement = statement
        return SimpleNamespace(all=lambda: [])


class _FakeIndices:
    """``client.indices`` 的记账替身：只记「发了什么」，不连集群。"""

    def __init__(self) -> None:
        self.exists_result = True
        self.exists_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.put_mapping_calls: list[dict] = []

    async def exists(self, **kwargs: Any) -> bool:
        self.exists_calls.append(kwargs)
        return self.exists_result

    async def create(self, **kwargs: Any) -> None:
        self.create_calls.append(kwargs)

    async def put_mapping(self, **kwargs: Any) -> None:
        self.put_mapping_calls.append(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.indices = _FakeIndices()
        self.bulk_calls: list[dict] = []

    async def bulk(self, **kwargs: Any) -> dict:
        self.bulk_calls.append(kwargs)
        return {"errors": False}


def _KeywordProvider(client: _FakeClient) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://es.example:9200", index_name="rag_chunks", client=client
    )


def _chunk(chunk_id: str, **overrides: Any) -> dict:
    """索引编排交出来的切片字典（只留关键词侧要用的那几个键）。"""

    chunk = {
        "id": chunk_id,
        "tenant_id": TENANT,
        "kb_id": KB,
        "document_id": "doc-1",
        "content": "退款申请流程：提交工单后 3 个工作日内到账。",
        "retrieval_text": "文档：售后手册 章节：退款 > 流程 正文：退款申请流程。",
        "index_version": "v1",
        "meta": {"title": "售后手册"},
    }
    chunk.update(overrides)
    return chunk


# ───────────────────────────────────────────────────── 一、向量召回
# 「同一文档两稿各一份切片、指针指向 v2」这条验收就钉在这里和后面的
# ``test_only_the_two_recall_paths_deliver_the_current_draft``：向量路的谓词是
# 「切片稿号 = 文档当前稿指针」，一个 JOIN 就把「谁是当前稿」交给库算，应用层不缓存指针。


def test_vector_recall_reads_only_the_current_draft() -> None:
    """范围版召回：``chunks.doc_version = documents.current_version`` 必须在 WHERE 里。"""

    session = _CapturingSession()

    rows = asyncio.run(
        PgVectorStore().similarity_search_scope(
            session,
            [0.1, 0.2, 0.3],
            tenant_id=TENANT,
            index_versions={KB: "v1"},
            top_k=5,
        )
    )

    assert rows == []
    sql = _sql(session.statement)
    assert "JOIN documents ON documents.id = chunks.document_id" in sql, (
        "当前稿指针在文档行上：不 JOIN 就没有“谁是当前稿”这个事实"
    )
    assert "chunks.doc_version = documents.current_version" in sql, (
        "少了这一条，旧稿切片会以同等分数参与 TopK 竞争（ADR-0005 决策：默认只查当前稿）"
    )


def test_single_kb_vector_recall_uses_the_same_current_draft_predicate() -> None:
    """单库版召回（``similarity_search``）与范围版同一个口径：两条 SQL 不能各写一套。"""

    session = _CapturingSession()

    asyncio.run(
        PgVectorStore().similarity_search(
            session,
            [0.1, 0.2, 0.3],
            tenant_id=TENANT,
            kb_id=KB,
            top_k=5,
        )
    )

    sql = _sql(session.statement)
    assert "chunks.doc_version = documents.current_version" in sql


# ─────────────────────────────────────────────────── 二、关键词召回
# 关键词侧**只用一个布尔 term**：父 spec 否决了「成对枚举 (document_id, doc_version)
# 子句」——库里文档一多，查询体就会跟着文档数一起长。布尔字段的名字是 ``is_current``，
# 值在索引期算出来（写入侧见 ``indexing_service``）。


def test_keyword_recall_filters_on_a_single_is_current_term() -> None:
    """召回只认 ``is_current: true``：一个 term，不枚举文档。"""

    query = build_bm25_query(
        tenant_id=TENANT,
        index_versions={KB: "v1"},
        search_query="退款申请流程",
        top_k=5,
    )
    filters = query["query"]["bool"]["filter"]

    assert {"term": {"is_current": True}} in filters, "当前稿条件必须落在 filter 里"
    assert len(filters) == 3, "租户 + 库/版本配对 + 当前稿，三条过滤一个不多一个不少"
    # 只看**过滤子句**：``_source`` 白名单里本来就有 ``document_id``（回包要给归属），
    # 这里禁的是「按文档枚举」的过滤条件——那会把查询体撑成与库规模同阶。
    filters_json = json.dumps(filters)
    assert '"document_id"' not in filters_json, "按文档枚举会随库规模膨胀（父 spec 明确否决）"
    assert '"doc_version"' not in filters_json, (
        "成对枚举 (document_id, doc_version) 是另一种膨胀：当前稿只用布尔 term 表达"
    )


def test_keyword_mapping_declares_the_current_draft_fields() -> None:
    """mapping 里要有这两个字段，否则 term 打到未映射字段上是 0 条命中。"""

    properties = build_es_mapping()["mappings"]["properties"]

    assert properties["is_current"] == {"type": "boolean"}, "只做精确匹配，布尔即可"
    assert properties["doc_version"] == {"type": "keyword"}, (
        "稿号是过滤维度：精确匹配，不分词（以后按稿直查要用）"
    )


def test_an_existing_physical_index_gets_the_additive_mapping() -> None:
    """第 11 步建好的物理索引没有这两个字段：``ensure_index`` 要补一次 mapping。

    补的是 **mapping 不是数据**：这是 additive ``put_mapping``，不重建索引、不动已有
    文档。少了这一步，``is_current`` 的 term 会命中 0 条——关键词路静默全空，而向量路
    照常返回结果，症状只是「混合检索少了一路」。
    """

    client = _FakeClient()
    client.indices.exists_result = True  # 物理索引是上一版就建好的
    provider = _KeywordProvider(client)

    asyncio.run(provider.ensure_index("rag_chunks__v1"))

    assert client.indices.create_calls == [], "索引已存在：不重建"
    assert client.indices.put_mapping_calls == [
        {
            "index": "rag_chunks__v1",
            "properties": {
                "doc_version": {"type": "keyword"},
                "is_current": {"type": "boolean"},
            },
        }
    ], "补的是这两个字段，且是 properties 形式的 additive put_mapping"


def test_a_freshly_created_index_does_not_need_the_patch() -> None:
    """新建的索引 mapping 里就带着两个字段：再补一次是白跑一趟。"""

    client = _FakeClient()
    client.indices.exists_result = False
    provider = _KeywordProvider(client)

    asyncio.run(provider.ensure_index("rag_chunks__v2"))

    assert len(client.indices.create_calls) == 1
    assert client.indices.put_mapping_calls == [], "建索引时已经带上这两个字段了"


def test_the_mapping_patch_is_applied_once_per_process() -> None:
    """同一张物理索引只补一次：``put_mapping`` 幂等，但没必要每次写入都发一趟。"""

    client = _FakeClient()
    client.indices.exists_result = True
    provider = _KeywordProvider(client)

    asyncio.run(provider.ensure_index("rag_chunks__v1"))
    asyncio.run(provider.ensure_index("rag_chunks__v1"))

    assert len(client.indices.put_mapping_calls) == 1


def test_a_failed_mapping_patch_does_not_fail_indexing() -> None:
    """补 mapping 失败不升级成「这篇文档索引失败」：它是补丁，不是写入本身。

    失败的后果（关键词路少一路结果）由日志留痕；让整篇文档因为一次元数据补丁而失败，
    代价比它治的问题大得多。失败也**不记进已补集合**，下一次写入会重试。
    """

    class _ReadOnlyIndices(_FakeIndices):
        async def put_mapping(self, **kwargs: Any) -> None:
            self.put_mapping_calls.append(kwargs)
            raise RuntimeError("cluster is read-only")

    client = _FakeClient()
    client.indices = _ReadOnlyIndices()
    client.indices.exists_result = True
    provider = _KeywordProvider(client)

    asyncio.run(provider.ensure_index("rag_chunks__v1"))
    asyncio.run(provider.add_chunks([_chunk("chunk-1")]))

    assert len(client.indices.put_mapping_calls) >= 1, "确实试过补"
    assert client.bulk_calls, "写入照旧发生：补丁失败不挡住索引"


def test_the_mapping_patch_is_additive_properties_only() -> None:
    """补 mapping 的载荷只含 ``properties``：带 settings / analysis 会被 ES 拒绝。"""

    assert build_mapping_update() == {
        "properties": {
            "doc_version": {"type": "keyword"},
            "is_current": {"type": "boolean"},
        }
    }
    assert set(build_mapping_update()) == {"properties"}


def test_keyword_documents_carry_the_current_draft_marker() -> None:
    """写入侧：切片字典顶层的 ``doc_version`` / ``is_current`` 透传到 ES 文档。"""

    documents = build_keyword_documents(
        [
            _chunk("chunk-v2", doc_version=2, is_current=True),
            _chunk("chunk-v1", doc_version=1, is_current=False),
        ]
    )

    assert [(doc["chunk_id"], doc["doc_version"], doc["is_current"]) for doc in documents] == [
        ("chunk-v2", 2, True),
        ("chunk-v1", 1, False),
    ]


def test_keyword_documents_default_the_marker_to_the_first_draft() -> None:
    """没带这两个键的老调用点按第 1 稿 / 当前稿解释，绝不写空值。"""

    document = build_keyword_documents([_chunk("chunk-1")])[0]

    assert document["doc_version"] == DEFAULT_DOC_VERSION == 1
    assert document["is_current"] is True


def test_without_a_keyword_engine_the_current_draft_dimension_is_not_judged() -> None:
    """能力不存在 → 这一维**不参与**：不报错、不假装有索引可补。

    「能力不存在」与「数据缺失」必须分开：空实现既没有字面可补的 mapping，也不该让
    写入侧因为多了 ``is_current`` 这个键而失败。
    """

    provider = NoopKeywordSearchProvider()

    assert provider.available is False
    assert not hasattr(provider, "ensure_index"), "没有索引这回事，就没有 mapping 可补"
    assert (
        asyncio.run(provider.add_chunks([_chunk("chunk-1", doc_version=1, is_current=True)]))
        is None
    )
