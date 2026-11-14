"""第 1 步双入口骨架的验收：占位 Key / 列库 / kb_ids / MCP 工具。

唯一接缝仍是 ASGI 应用边界（TestClient）。除「缺 Key」那条用独立 app
测真实 get_current_key 外，其余请求都真实带占位 Key 头；数据库与 Provider
用 dependency_overrides + monkeypatch 换掉，全程离线、不发真实模型请求。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from tests import fake_db
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb

RETRIEVE_PATH = "/api/v1/rag/retrieve"
LIST_KB_PATH = "/api/v1/knowledge-bases"
AUTH_HEADERS = {"Authorization": f"Bearer {settings.DEV_API_KEY}"}


@pytest.fixture
def client():
    """只替换数据库依赖；鉴权走真实的占位 Key 校验。

    会话入口一次换全（``override_all_db``）：票 #130 之后身份判定与写事务都从
    ``get_async_db`` 取会话，只覆写 ``get_db`` 的话它们会去连真库。
    """

    main = importlib.import_module("app.main")

    fake_db.override_all_db(main.app, EmptySession())

    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def test_missing_or_wrong_dev_key_returns_business_code() -> None:
    """头部没带占位 Key → 20010（业务失败 HTTP 200，不是 401）。"""

    from app.api.error_handlers import install_handlers
    from app.api.router import api_router

    standalone = FastAPI()
    standalone.include_router(api_router)
    install_handlers(standalone)

    payload = {"kb_ids": ["kb-1"], "query": "hello"}

    with TestClient(standalone, raise_server_exceptions=False) as test_client:
        missing = test_client.post(RETRIEVE_PATH, json=payload)
        assert missing.status_code == 200
        assert missing.json()["code"] == 20010

        wrong = test_client.post(
            RETRIEVE_PATH, json=payload, headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 200
        assert wrong.json()["code"] == 20010


def test_tenant_id_in_body_is_rejected(client: TestClient) -> None:
    """tenant_id 不再由调用方传；硬塞进来要被模型拒掉（20002）。"""

    response = client.post(
        RETRIEVE_PATH,
        json={"kb_ids": ["kb-1"], "query": "hello", "tenant_id": "evil-tenant"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["code"] == 20002


def test_list_knowledge_bases_returns_only_default_tenant(client, monkeypatch) -> None:
    """GET /api/v1/knowledge-bases 列这把 Key 可见的库。"""

    repo = importlib.import_module("app.repositories.knowledge_base_repository")

    async def fake_list(db, tenant_id):
        """列库是协程：路由读的是 ``await list_by_tenant(...)``。"""

        assert tenant_id == settings.DEV_TENANT_ID
        # 模拟的是 knowledge_bases 表的行：切块配置是库级字段，行里必须有。
        return [
            SimpleNamespace(
                id="kb-1",
                name="退款政策",
                tenant_id=tenant_id,
                split_strategy="structure",
                allowed_formats=["markdown"],
                settings={"chunk_size": 800},
            ),
            SimpleNamespace(
                id="kb-2",
                name="换货政策",
                tenant_id=tenant_id,
                split_strategy="size",
                allowed_formats=["markdown", "text", "docx", "pdf", "html"],
                settings={"chunk_size": 400},
            ),
        ]

    monkeypatch.setattr(repo, "list_by_tenant", fake_list)

    response = client.get(LIST_KB_PATH, headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert [item["kb_id"] for item in body["data"]["knowledge_bases"]] == ["kb-1", "kb-2"]
    # 切块配置要能在这把 Key 下看到：检索结果异常时先排除「切法选错了」。
    assert [item["split_strategy"] for item in body["data"]["knowledge_bases"]] == [
        "structure",
        "size",
    ]
    assert [item["chunk_size"] for item in body["data"]["knowledge_bases"]] == [800, 400]


def test_retrieve_covers_every_kb_in_one_scope_call(client, monkeypatch) -> None:
    """``kb_ids`` 是列表：一次范围召回覆盖全部库，再按分数排序后截到 TOP_K。

    票 5 起不再是「逐库检索再合并」：分库召回会让不相关的库也各占名额。这里的
    断言因此变成「一次调用 + 配对映射覆盖两个库」，合并排序的口径不变。
    """

    rag_service = importlib.import_module("app.services.rag_service")
    knowledge_base_access = importlib.import_module("app.services.knowledge_base_access")

    monkeypatch.setattr(
        knowledge_base_access.knowledge_base_repository,
        "get_by_id_for_tenant",
        async_visible_kb,
    )

    class FakeEmbedding:
        async def embed_query(self, query):
            return [0.1, 0.2, 0.3]

    class FakeVectorStore:
        def __init__(self):
            self.scopes: list[dict[str, str]] = []

        async def similarity_search_scope(
            self, db, query_vector, *, tenant_id, index_versions, top_k
        ):
            self.scopes.append(dict(index_versions))
            rows = []
            for kb_id in index_versions:
                score = 0.9 if kb_id == "kb-a" else 0.5
                rows.append(
                    {
                        "document_id": f"doc-{kb_id}",
                        "chunk_id": f"chunk-{kb_id}",
                        "kb_id": kb_id,
                        "title": "title",
                        "content": "content",
                        "score": score,
                    }
                )
            return rows

        async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k):
            raise AssertionError("范围版一次就能覆盖全部库，不许按库循环")

    fake_store = FakeVectorStore()
    monkeypatch.setattr(rag_service, "get_embedding_provider", lambda: FakeEmbedding())
    monkeypatch.setattr(rag_service, "get_vector_store", lambda: fake_store)

    response = client.post(
        RETRIEVE_PATH,
        json={"kb_ids": ["kb-b", "kb-a"], "query": "hello"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["kb_ids"] == ["kb-b", "kb-a"]
    assert fake_store.scopes == [{"kb-b": "v1", "kb-a": "v1"}], (
        "一次范围调用；每个显式传入的库都在配对映射里"
    )
    chunks = body["data"]["retrieved_chunks"]
    assert [c["document_id"] for c in chunks] == ["doc-kb-a", "doc-kb-b"], "按分数合并排序"
    assert [c["kb_id"] for c in chunks] == ["kb-a", "kb-b"], "每条切片都能归属到某个库"


def test_mcp_tools_have_the_declared_signatures() -> None:
    """三个工具第 1 步就挂好；两个数据面工具都只暴露 kb_ids + query。"""

    import asyncio

    from app.mcp_server import create_mcp_server

    class FakeSession:
        def close(self):
            pass

    async def fake_retrieve(db, *, tenant_id, kb_ids, query, options):
        # 票 #77 起组合根还收一个已解析的 ``options``（模式 / 条数 / 两个形状开关）；
        # 这条用例只关心工具的**签名**，回调里原样收下即可。
        assert options is not None
        return {"query": query, "kb_ids": kb_ids, "retrieved_chunks": [], "metadata": {}}

    async def fake_list(db, tenant_id):
        return []

    mcp = create_mcp_server(
        get_async_db=lambda: AsyncSessionAdapter(FakeSession()),
        retrieve=fake_retrieve,
        list_kbs=fake_list,
    )
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}

    assert set(tools) == {"rag_retrieve", "rag_list_knowledge_bases", "rag_research"}
    params = getattr(tools["rag_retrieve"], "parameters", {}) or {}
    assert set(params.get("properties", {})) == {"kb_ids", "query"}
    # 第三个工具与它字面同形（票 #151）：数据面两个工具的签名只表达「问什么、查哪些库」。
    research_params = getattr(tools["rag_research"], "parameters", {}) or {}
    assert set(research_params.get("properties", {})) == {"kb_ids", "query"}
