"""第 1 步双入口骨架的验收：占位 Key / 列库 / kb_ids / MCP 工具。

唯一接缝仍是 ASGI 应用边界（TestClient）。除「缺 Key」那条用独立 app
测真实 resolve_dev_key 外，其余请求都真实带占位 Key 头；数据库与 Provider
用 dependency_overrides + monkeypatch 换掉，全程离线、不发真实模型请求。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings

RETRIEVE_PATH = "/api/v1/rag/retrieve"
LIST_KB_PATH = "/api/v1/knowledge-bases"
AUTH_HEADERS = {"Authorization": f"Bearer {settings.DEV_API_KEY}"}


@pytest.fixture
def client():
    """只替换数据库依赖；鉴权走真实的占位 Key 校验。"""

    main = importlib.import_module("app.main")
    session = importlib.import_module("app.db.session")

    main.app.dependency_overrides[session.get_db] = lambda: object()
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

    def fake_list(db, tenant_id):
        assert tenant_id == settings.DEV_TENANT_ID
        return [
            SimpleNamespace(id="kb-1", name="退款政策", tenant_id=tenant_id),
            SimpleNamespace(id="kb-2", name="换货政策", tenant_id=tenant_id),
        ]

    monkeypatch.setattr(repo, "list_by_tenant", fake_list)

    response = client.get(LIST_KB_PATH, headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert [item["kb_id"] for item in body["data"]["knowledge_bases"]] == ["kb-1", "kb-2"]


def test_retrieve_traverses_every_kb_id(client, monkeypatch) -> None:
    """kb_ids 是列表：逐库检索、合并、按分数排序后截到 TOP_K。"""

    rag_service = importlib.import_module("app.services.rag_service")

    monkeypatch.setattr(
        rag_service.knowledge_base_repository,
        "get_by_id",
        lambda db, kb_id: SimpleNamespace(tenant_id=settings.DEV_TENANT_ID),
    )

    class FakeEmbedding:
        async def embed_query(self, query):
            return [0.1, 0.2, 0.3]

    class FakeVectorStore:
        def __init__(self):
            self.seen: list[str] = []

        async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k):
            self.seen.append(kb_id)
            score = 0.9 if kb_id == "kb-a" else 0.5
            return [
                {
                    "document_id": f"doc-{kb_id}",
                    "chunk_id": f"chunk-{kb_id}",
                    "title": "title",
                    "content": "content",
                    "score": score,
                }
            ]

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
    assert fake_store.seen == ["kb-b", "kb-a"], "每个显式传入的库都要被检索"
    chunks = body["data"]["retrieved_chunks"]
    assert [c["document_id"] for c in chunks] == ["doc-kb-a", "doc-kb-b"], "按分数合并排序"


def test_mcp_tools_have_the_declared_signatures() -> None:
    """两个工具第 1 步就挂好；rag_retrieve 暴露 kb_ids + query。"""

    import asyncio

    from app.mcp_server import create_mcp_server

    class FakeSession:
        def close(self):
            pass

    async def fake_retrieve(db, *, tenant_id, kb_ids, query):
        return {"query": query, "kb_ids": kb_ids, "retrieved_chunks": [], "metadata": {}}

    def fake_list(db, tenant_id):
        return []

    mcp = create_mcp_server(get_db=FakeSession, retrieve=fake_retrieve, list_kbs=fake_list)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}

    assert set(tools) == {"rag_retrieve", "rag_list_knowledge_bases"}
    params = getattr(tools["rag_retrieve"], "parameters", {}) or {}
    assert set(params.get("properties", {})) == {"kb_ids", "query"}
