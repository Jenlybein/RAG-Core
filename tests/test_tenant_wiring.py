"""票 #76：两个入口接线 + 数据范围白名单 + 隔离收口。

接缝是**两条真实入口**：HTTP 走 ``/api/v1/*``，MCP 走 ``/mcp/`` 的 JSON-RPC
（``initialize`` → ``tools/call``）。两条入口都挂在同一个应用上，身份走**真实的判定内核**
（``app.core.auth.resolve_identity``），只是它读到的库行是假的——``api_keys`` / ``tenants``
/ ``key_policies`` / ``plans`` 四张表的行由 :class:`tests.fake_db.RecordingSession` 提供，
套餐缓存换成注入的假 Redis。默认套件因此不碰 PostgreSQL、不碰 Redis、不碰 Elasticsearch。

为什么身份必须走真内核：本票要证明的正是「两边只有一份判定」。若在 MCP 一侧把
``resolve_identity`` 换掉，被测掉的恰好就是这份判定本身。

**同源断言比的是整份 data 载荷**：两份载荷相等，于是后来往契约里加的每一栏（例如票
#78 的 ``metadata.tenant_policy``）都自动落进这条断言里，不必回来改测试——回显只在一边
出现，这里当场就红。
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.utilities.lifespan import combine_lifespans

from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.models.api_key import ApiKey
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.models.key_policy import PROFILE_QUALITY, PROFILE_SPEED, KeyPolicy
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO, PLAN_STANDARD, Plan, plan_row_of
from app.models.tenant import Tenant
from tests.fake_db import AsyncSessionAdapter, RecordingSession

TENANT_ID = "tenant_acme"
OTHER_TENANT_ID = "tenant_other"
POLICY_ID = "22222222-2222-4222-8222-222222222222"
KEY_ID = "33333333-3333-4333-8333-333333333333"
KEY_VALUE = "rc_live_0123456789abcdef0123456789abcdef"
PLAN_IDS = {PLAN_FREE: 1, PLAN_STANDARD: 2, PLAN_PRO: 3}

KB_IN_SCOPE = "kb-in-scope"
KB_OUT_OF_SCOPE = "kb-out-of-scope"
DOC_IN_SCOPE = "doc-in-scope"
DOC_OUT_OF_SCOPE = "doc-out-of-scope"

RETRIEVE_PATH = "/api/v1/rag/retrieve"
LIST_KB_PATH = "/api/v1/knowledge-bases"
LIST_DOCS_PATH = "/api/v1/documents"
UPLOAD_PATH = "/api/v1/documents/upload"
MCP_PATH = "/mcp/"
JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


# ───────────────────────────────────────────── 假库行：身份由真内核从这些行里解出来


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for code, plan_id in PLAN_IDS.items():
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = plan_id
        rows.append(row)
    return rows


def _key_row(*, tenant_id: str = TENANT_ID) -> ApiKey:
    return ApiKey(
        id=KEY_ID,
        tenant_id=tenant_id,
        policy_id=POLICY_ID,
        key_value=KEY_VALUE,
        label="外部应用",
        status="active",
    )


def _tenant_row(*, tenant_id: str = TENANT_ID, plan_code: str = PLAN_PRO) -> Tenant:
    return Tenant(id=tenant_id, name="Acme 公司", plan_id=PLAN_IDS[plan_code], status="active")


def _policy_row(*, allowed_kb_ids: list[str], profile: str = PROFILE_SPEED) -> KeyPolicy:
    return KeyPolicy(
        id=POLICY_ID,
        tenant_id=TENANT_ID,
        name="外部应用",
        allowed_kb_ids=list(allowed_kb_ids),
        research_allowed=False,
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=10,
        retrieve_daily=100,
        profile=profile,
        include_parent=True,
        expand_table_rows=True,
        custom_options={},
    )


def _kb_row(kb_id: str, *, tenant_id: str = TENANT_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=kb_id,
        tenant_id=tenant_id,
        name=f"库 {kb_id}",
        description=None,
        split_strategy="size",
        allowed_formats=["markdown", "text"],
        settings={"chunk_size": 400},
        # 检索范围从库行里读生效版本（票 #141）：缺这一列等于「还没回填的库」。
        active_index_version=DEFAULT_INDEX_VERSION,
    )


def _doc_row(document_id: str, *, kb_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=document_id,
        kb_id=kb_id,
        tenant_id=TENANT_ID,
        title="手册",
        status=2,
        source_type="markdown",
        source_file_path=None,
        index_claimed_at=None,
        error_message=None,
    )


def _session(
    *,
    allowed_kb_ids: list[str],
    kb_ids: list[str],
    profile: str = PROFILE_SPEED,
    plan_code: str = PLAN_PRO,
) -> RecordingSession:
    """一份假会话：既供判定内核读身份，也供列库读 ``knowledge_bases`` 行。"""

    return RecordingSession(
        rows={
            Plan: _plan_rows(),
            ApiKey: [_key_row()],
            Tenant: [_tenant_row(plan_code=plan_code)],
            KeyPolicy: [_policy_row(allowed_kb_ids=allowed_kb_ids, profile=profile)],
            # 列库按租户过滤：这里预置「本项目租户 + 别家租户」各一个库。
            KnowledgeBase: [_kb_row(kb_id) for kb_id in kb_ids]
            + [_kb_row(KB_OUT_OF_SCOPE, tenant_id=OTHER_TENANT_ID)],
        }
    )


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """两路都不发散：每个在范围里的库各回一条切片，分数按传入顺序递减。"""

    async def similarity_search_scope(
        self, _db: Any, _vector: Any, *, tenant_id: str, index_versions: dict, top_k: int
    ) -> list[dict]:
        return [
            {
                "document_id": f"doc-{kb_id}",
                "chunk_id": f"chunk-{kb_id}",
                "kb_id": kb_id,
                "title": "手册",
                "content": f"{kb_id} 的内容",
                "score": 0.9 - index * 0.1,
            }
            for index, kb_id in enumerate(index_versions)
        ]

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        raise AssertionError("范围版一次覆盖全部库，不许按库循环")


class _FakeRedis:
    """套餐缓存用的最小 Redis 替身（注入，不是 monkeypatch 全局）。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _RedisPlanCache:
    """只认 plan id 的内存缓存：本票不关心缓存策略，别让它去连真 Redis。

    三个方法都是 ``async``（票 #129）：生产实现握的是 ``redis.asyncio`` 客户端，替身必须
    同形，否则「忘了 await」会变成一条 500 而不是「缓存没命中」。
    """

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}

    async def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    async def set(self, plan_id: int, plan: Any) -> None:
        self.items[plan_id] = plan

    async def delete(self, plan_id: int) -> None:
        self.items.pop(plan_id, None)


class EntryClient:
    """一次测试里的两条入口：同一个应用实例、同一个客户端。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {KEY_VALUE}"}
        self._mcp_headers: dict[str, str] = {}

    # ── HTTP ──
    def retrieve(self, kb_ids: list[str], *, headers: dict[str, str] | None = None) -> dict:
        response = self.client.post(
            RETRIEVE_PATH,
            json={"kb_ids": kb_ids, "query": "退款多久到账"},
            headers=self.headers if headers is None else headers,
        )
        assert response.status_code == 200
        return response.json()

    # ── MCP ──
    def mcp_call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """跑一次 ``tools/call``；返回整个 JSON-RPC result。"""

        call_headers = dict(JSONRPC_HEADERS)
        call_headers.update(self._mcp_headers)
        call_headers.update(self.headers if headers is None else headers)
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=call_headers,
        )
        assert response.status_code == 200
        return response.json()["result"]

    def initialize_mcp(self) -> None:
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t76", "version": "0"},
                },
            },
            headers=JSONRPC_HEADERS,
        )
        assert response.status_code == 200
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._mcp_headers["mcp-session-id"] = session_id
        self.client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**JSONRPC_HEADERS, **self._mcp_headers},
        )


def _mcp_code(result: dict) -> int:
    """MCP 侧的业务码：业务失败按工具执行失败表达，文本里带 ``<code>|<msg>``。

    fastmcp 会在文本前加一句「Error calling tool '...'」，所以按 ``<code>|`` 找那一段，
    不去假设整段文本从码开始。
    """

    if not result.get("isError"):
        return int(ErrorCode.SUCCESS)
    text = result["content"][0]["text"]
    match = re.search(r"(\d{5})\|", text)
    assert match, f"MCP 失败结果里没有错误码：{text!r}"
    return int(match.group(1))


def _mcp_payload(result: dict) -> dict:
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def entries(monkeypatch: pytest.MonkeyPatch):
    """两条入口 + 一份受限的密钥配置（白名单只有 ``KB_IN_SCOPE``）。"""

    yield from _entries_with(monkeypatch, allowed_kb_ids=[KB_IN_SCOPE])


@pytest.fixture
def unrestricted_entries(monkeypatch: pytest.MonkeyPatch):
    """内置开发身份：不做范围收窄，跨租户仍由库行的 ``tenant_id`` 挡住。"""

    yield from _entries_with(monkeypatch, allowed_kb_ids=None)


@pytest.fixture
def quality_on_free(monkeypatch: pytest.MonkeyPatch):
    """一把挂着 ``quality`` 的 Key + 免费套餐：档位门禁在两条入口上都该拒绝。"""

    yield from _entries_with(
        monkeypatch, allowed_kb_ids=[KB_IN_SCOPE], profile=PROFILE_QUALITY, plan_code=PLAN_FREE
    )


def _entries_with(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed_kb_ids: list[str] | None,
    profile: str = PROFILE_SPEED,
    plan_code: str = PLAN_PRO,
):
    """搭一个同时挂 HTTP 与 MCP 的应用；``allowed_kb_ids=None`` = 内置开发身份。"""

    main = importlib.import_module("app.api.router")
    error_handlers = importlib.import_module("app.api.error_handlers")
    async_session = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    doc_repo = importlib.import_module("app.repositories.document_repository")
    mcp_server = importlib.import_module("app.mcp_server")

    def _get_db() -> RecordingSession:
        return _session(
            allowed_kb_ids=[] if allowed_kb_ids is None else allowed_kb_ids,
            kb_ids=[KB_IN_SCOPE],
            profile=profile,
            plan_code=plan_code,
        )

    # 内置开发身份的租户标识来自服务端配置，与假行里的租户不同（它就是「本地那一个」）。
    caller_tenant = settings.DEV_TENANT_ID if allowed_kb_ids is None else TENANT_ID

    # 白名单外的库对「跨租户」这一条要能返回 None：它属于别家租户。
    async def _visible_kb(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        """取库的替身是**协程**（票 #130：可见性守卫只剩异步的那一个入口）。"""

        if kb_id == KB_IN_SCOPE and tenant_id == caller_tenant:
            return _kb_row(kb_id)
        return None

    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _visible_kb)

    async def _visible_doc(_db: Any, document_id: str, *, tenant_id: str) -> Any:
        """按 id 取文档的替身是**协程**（票 #130：重建 / 删除路由读异步仓储）。"""

        if tenant_id != caller_tenant:
            return None
        kb_id = KB_IN_SCOPE if document_id == DOC_IN_SCOPE else KB_OUT_OF_SCOPE
        return _doc_row(document_id, kb_id=kb_id)

    monkeypatch.setattr(doc_repo, "get_by_id_for_tenant_async", _visible_doc)
    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore())

    plan_cache = _RedisPlanCache()
    monkeypatch.setattr(
        importlib.import_module("app.services.plan_service"),
        "default_plan_cache",
        lambda: plan_cache,
    )
    # 两种模式都只走向量一路：默认值是服务端配置，两条入口读的是同一个值，
    # 因此「同源」不受影响，而替身面小一圈。
    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "vector")

    if allowed_kb_ids is None:
        # 内置开发身份：开关关着 + 占位 token。
        monkeypatch.setattr(settings, "AUTH_ENABLED", False)
        bearer = settings.DEV_API_KEY
    else:
        monkeypatch.setattr(settings, "AUTH_ENABLED", True)
        bearer = KEY_VALUE

    mcp_app = mcp_server.create_mcp_http_app(
        # 检索链路用异步会话（第 11 步票 #127）：同一个替身，包一层异步面。
        get_async_db=lambda: AsyncSessionAdapter(_get_db()),
        retrieve=service.retrieve,
        list_kbs=kb_repo.list_by_tenant,
    )
    app = FastAPI(lifespan=combine_lifespans(mcp_app.lifespan))
    app.include_router(main.api_router)
    error_handlers.install_handlers(app)
    app.mount("/mcp", mcp_app)
    app.dependency_overrides[async_session.get_async_db] = lambda: AsyncSessionAdapter(_get_db())

    with TestClient(app) as client:
        entry = EntryClient(client)
        entry.headers = {"Authorization": f"Bearer {bearer}"}
        entry.initialize_mcp()
        try:
            yield entry
        finally:
            app.dependency_overrides.clear()


# ───────────────────────────────────────────── 白名单：两条入口同一个答案


def test_retrieve_serves_a_whitelisted_knowledge_base(entries: EntryClient) -> None:
    body = entries.retrieve([KB_IN_SCOPE])

    assert body["code"] == 0
    assert [chunk["kb_id"] for chunk in body["data"]["retrieved_chunks"]] == [KB_IN_SCOPE]


def test_retrieve_rejects_an_out_of_whitelist_knowledge_base(entries: EntryClient) -> None:
    """白名单外的库 → 「知识库不存在」（20008），不是「无权限」。"""

    body = entries.retrieve([KB_OUT_OF_SCOPE])

    assert body["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert body["data"] is None


def test_retrieve_rejects_a_mixed_request_that_mentions_an_out_of_scope_kb(
    entries: EntryClient,
) -> None:
    """一次请求里混进一个越界的库，整句拒绝——不偷偷把那个库摘掉。"""

    body = entries.retrieve([KB_IN_SCOPE, KB_OUT_OF_SCOPE])

    assert body["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


def test_a_cross_tenant_kb_is_indistinguishable_from_a_missing_one(
    unrestricted_entries: EntryClient,
) -> None:
    """跨租户与「不存在」给出完全一样的响应体：可区分就等于泄露存在性。"""

    cross_tenant = unrestricted_entries.retrieve([KB_OUT_OF_SCOPE])

    assert cross_tenant["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert cross_tenant["msg"] == "knowledge base not found"


def test_the_gate_rejects_before_the_retrieval_burns_an_embedding(
    entries: EntryClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单关卡在第二道：它先于召回，被拒的请求连一次嵌入都不该烧。"""

    embedding_calls: list[str] = []

    class _RecordingEmbedding(_FakeEmbedding):
        async def embed_query(self, query: str) -> list[float]:
            embedding_calls.append(query)
            return await super().embed_query(query)

    service = importlib.import_module("app.services.rag_service")
    monkeypatch.setattr(service, "get_embedding_provider", lambda: _RecordingEmbedding())

    assert entries.retrieve([KB_OUT_OF_SCOPE])["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert embedding_calls == [], "拒绝先于召回"

    assert entries.retrieve([KB_IN_SCOPE])["code"] == 0
    assert embedding_calls == ["退款多久到账"], "放行的请求才真的走链路"


def test_listing_knowledge_bases_hides_what_the_key_cannot_see(entries: EntryClient) -> None:
    response = entries.client.get(LIST_KB_PATH, headers=entries.headers)

    body = response.json()
    assert body["code"] == 0
    assert [item["kb_id"] for item in body["data"]["knowledge_bases"]] == [KB_IN_SCOPE]


def test_document_listing_rejects_an_out_of_whitelist_kb(entries: EntryClient) -> None:
    response = entries.client.get(
        LIST_DOCS_PATH, params={"kb_id": KB_OUT_OF_SCOPE}, headers=entries.headers
    )

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


def test_upload_rejects_an_out_of_whitelist_kb(entries: EntryClient) -> None:
    response = entries.client.post(
        UPLOAD_PATH,
        json={"kb_id": KB_OUT_OF_SCOPE, "title": "手册", "content": "正文"},
        headers=entries.headers,
    )

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


def test_reindex_rejects_a_document_whose_kb_is_out_of_scope(entries: EntryClient) -> None:
    """重建按 ``document_id`` 进来，范围要看它所属的库。"""

    response = entries.client.post(
        f"/api/v1/documents/{DOC_OUT_OF_SCOPE}/reindex", headers=entries.headers
    )

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


def test_delete_rejects_a_document_whose_kb_is_out_of_scope(entries: EntryClient) -> None:
    response = entries.client.delete(
        f"/api/v1/documents/{DOC_OUT_OF_SCOPE}", headers=entries.headers
    )

    assert response.json()["code"] == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


# ───────────────────────────────────────────── 内置开发身份：不收窄范围


def test_the_development_identity_is_not_scope_restricted(
    unrestricted_entries: EntryClient,
) -> None:
    """本地开发不造 Key 也能查：它的白名单是空的，但它显式声明不做范围收窄。"""

    body = unrestricted_entries.retrieve([KB_IN_SCOPE])

    assert body["code"] == 0
    assert [chunk["kb_id"] for chunk in body["data"]["retrieved_chunks"]] == [KB_IN_SCOPE]


# ───────────────────────────────────────────── MCP：同一套判定、同一套码


def test_mcp_rejects_a_request_without_a_bearer_token(entries: EntryClient) -> None:
    result = entries.mcp_call(
        "rag_retrieve", {"kb_ids": [KB_IN_SCOPE], "query": "q"}, headers={"Authorization": ""}
    )

    assert result["isError"] is True
    assert _mcp_code(result) == ErrorCode.UNAUTHORIZED


def test_mcp_rejects_a_disabled_key(entries: EntryClient) -> None:
    """四种未授权情形在 MCP 侧同样是业务失败（工具执行失败 + 码），不是协议错误。"""

    result = entries.mcp_call(
        "rag_retrieve",
        {"kb_ids": [KB_IN_SCOPE], "query": "q"},
        headers={"Authorization": "Bearer not-a-real-key"},
    )

    assert _mcp_code(result) == ErrorCode.UNAUTHORIZED


def test_mcp_serves_a_whitelisted_knowledge_base(entries: EntryClient) -> None:
    payload = _mcp_payload(
        entries.mcp_call("rag_retrieve", {"kb_ids": [KB_IN_SCOPE], "query": "q"})
    )

    assert [chunk["kb_id"] for chunk in payload["retrieved_chunks"]] == [KB_IN_SCOPE]


def test_mcp_rejects_an_out_of_whitelist_knowledge_base(entries: EntryClient) -> None:
    result = entries.mcp_call("rag_retrieve", {"kb_ids": [KB_OUT_OF_SCOPE], "query": "q"})

    assert result["isError"] is True
    assert _mcp_code(result) == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


def test_mcp_listing_hides_what_the_key_cannot_see(entries: EntryClient) -> None:
    payload = _mcp_payload(entries.mcp_call("rag_list_knowledge_bases", {}))

    assert [item["kb_id"] for item in payload["knowledge_bases"]] == [KB_IN_SCOPE]


# ───────────────────────────────────────────── 双入口同源


def test_both_entries_answer_the_same_question_the_same_way(entries: EntryClient) -> None:
    """同一把 Key、同一个问题：切片、业务码、回显全部一致——因为判定与业务只有一份。

    #101 起每条**响应**多一个 ``metadata.trace_id``，而它是**每次调用**的身份：两次
    独立的调用本来就不该拿到同一个值，所以整份载荷相等的断言把它摘掉再比。这不是放宽——
    下面单独钉住「两条入口都带、都是 32 位十六进制、而且不是同一个」，比原来的整份相等
    更严：原来那版根本不知道有这根线。
    """

    http_body = entries.retrieve([KB_IN_SCOPE])
    mcp_payload = _mcp_payload(
        entries.mcp_call("rag_retrieve", {"kb_ids": [KB_IN_SCOPE], "query": "退款多久到账"})
    )

    assert http_body["code"] == ErrorCode.SUCCESS

    http_trace_id = http_body["data"]["metadata"].pop("trace_id")
    mcp_trace_id = mcp_payload["metadata"].pop("trace_id")

    assert mcp_payload == http_body["data"], "整份载荷相等（tenant_policy 落地当天自动纳入）"
    assert http_body["data"]["retrieved_chunks"] == mcp_payload["retrieved_chunks"]

    # 两条入口各带各的那根线：形状相同、值不同（不同调用的 log_id 不同，派生自然不同）。
    for trace_id in (http_trace_id, mcp_trace_id):
        assert isinstance(trace_id, str)
        assert len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id)
    assert http_trace_id != mcp_trace_id, "两次独立调用不该共用一根线"


def test_both_entries_reject_the_same_scope_violation_with_the_same_code(
    entries: EntryClient,
) -> None:
    http_code = entries.retrieve([KB_OUT_OF_SCOPE])["code"]
    mcp_code = _mcp_code(
        entries.mcp_call("rag_retrieve", {"kb_ids": [KB_OUT_OF_SCOPE], "query": "q"})
    )

    assert http_code == mcp_code == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND


# ───────────────────────────────────────────── 档位门禁与回显：两条入口同一份判定


def test_both_entries_echo_the_tenant_policy_they_actually_ran(entries: EntryClient) -> None:
    """``tenant_policy`` 在两条入口上都出现，且逐字段相同（票 #78）。

    这条用**显式字段**再断言一遍，而不只靠 ``test_both_entries_answer_the_same_question_
    the_same_way`` 的整份载荷相等：整份相等证明「一致」，这里证明「这一栏确实存在且是
    那几项」——两者缺一，回显消失时后者仍然能报出来。
    """

    http_body = entries.retrieve([KB_IN_SCOPE])
    mcp_payload = _mcp_payload(
        entries.mcp_call("rag_retrieve", {"kb_ids": [KB_IN_SCOPE], "query": "退款多久到账"})
    )

    expected = {
        "plan": PLAN_PRO,
        "requested_profile": PROFILE_SPEED,
        "mode": "vector",
        "top_k": 5,
        "rerank": False,
        "rewrite": False,
        # 编排（#147）：两条入口回显的是同一份 ``options``，所以这个键也必须逐字相同。
        "orchestration": False,
        "kb_count": 1,
        "clipped": [],
    }
    assert http_body["data"]["metadata"]["tenant_policy"] == expected
    assert mcp_payload["metadata"]["tenant_policy"] == expected


def test_both_entries_reject_a_profile_the_plan_does_not_allow(
    quality_on_free: EntryClient,
) -> None:
    """档位门禁只有一份：同一把挂 ``quality`` 的 Key 打两扇门，都是 ``20013``。"""

    http_code = quality_on_free.retrieve([KB_IN_SCOPE])["code"]
    result = quality_on_free.mcp_call(
        "rag_retrieve", {"kb_ids": [KB_IN_SCOPE], "query": "退款多久到账"}
    )

    assert result["isError"] is True
    assert http_code == _mcp_code(result) == ErrorCode.FEATURE_NOT_ALLOWED


def test_each_entry_keeps_exactly_one_adapter_and_the_kernel_keeps_no_shim() -> None:
    """两个入口各自只剩一个薄适配器；内核里没有替路由代班的延迟导入。

    依赖函数改名是一次性收口：``Depends(...)`` 与 ``dependency_overrides[...]`` 认的是
    可调用对象本身，只改一半会让 override 静默失效、测试退化成真的走一遍鉴权。这条断言
    钉住「适配器里就这一个名字」，配合全仓 grep 一起守住改动完整性。
    """

    from app.api import deps
    from app.core import auth

    adapter_functions = {
        name
        for name in dir(deps)
        if not name.startswith("_")
        and inspect.isfunction(getattr(deps, name))
        and getattr(deps, name).__module__ == deps.__name__
    }
    assert adapter_functions == {"get_current_key"}

    # 内核里没有给路由代班的名字：占位时代那个依赖函数已经彻底搬去 ``deps``，
    # 只剩 ``DEV_*`` 常量（它们以常量名结尾，不叫某某 dependency）。
    assert not [name for name in dir(auth) if name.lower().endswith("dev_key")]


def test_mcp_tool_signatures_expose_no_privileged_fields() -> None:
    """签名里有什么，模型就能填什么：租户、套餐、档位、白名单一律不出现。"""

    import asyncio

    from app.mcp_server import create_mcp_server

    class _FakeSession:
        def close(self) -> None:
            pass

    async def _retrieve(*_args: Any, **_kwargs: Any) -> dict:
        return {}

    mcp = create_mcp_server(
        get_async_db=lambda: AsyncSessionAdapter(_FakeSession()),
        retrieve=_retrieve,
        list_kbs=lambda *_a, **_k: [],
    )
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert set(tools) == {"rag_retrieve", "rag_list_knowledge_bases"}
    properties = set(getattr(tools["rag_retrieve"], "parameters", {})["properties"])
    assert properties == {"kb_ids", "query"}

    privileged = {
        "tenant",
        "tenant_id",
        "plan",
        "plan_id",
        "profile",
        "policy",
        "allowed_kb_ids",
        "whitelist",
        "quota",
        "qps",
    }
    for tool in tools.values():
        names = set(getattr(tool, "parameters", {}).get("properties", {}))
        assert not names & privileged, f"{tool.name} 暴露了特权字段：{names & privileged}"
    assert not getattr(tools["rag_list_knowledge_bases"], "parameters", {}).get("properties")
