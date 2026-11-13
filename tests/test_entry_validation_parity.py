"""Gap 1（#72）：两条入口对同一份非法请求必须给出**同一个业务码**。

接缝是**两条真实入口**：HTTP 走 ``/api/v1/rag/retrieve``，MCP 走 `/mcp/` 的 JSON-RPC
``tools/call``。两者在同一个应用实例上，身份由同一份判定结论给出（不是两套假身份）。

这个测试之所以存在，是因为 MCP 的工具签名只有类型、没有约束：HTTP 的
``RetrieveRequest`` 用 ``Field(min_length=1)`` 挡住空字段，而 MCP 侧过去能把这些值一路
递进检索链路——同一个 ``{"kb_ids": ["kb-a"], "query": ""}`` 在 REST 上是 ``20002``，在
MCP 上却可能是一个 ``code: 0`` 的空结果。两条入口的契约是「同一把 Key、同样的错误码、
同样的 tenant_policy」，这条路必须由**同一份校验**守住，而不是各写一遍。

这里只测「非法请求怎么失败」：合法的双入口等价已有 ``test_tenant_wiring.py`` 覆盖。
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.utilities.lifespan import combine_lifespans

from app.core.auth import KeyContext
from app.core.exceptions import ErrorCode
from tests.fake_db import AsyncSessionAdapter, RecordingSession

RETRIEVE_PATH = "/api/v1/rag/retrieve"
MCP_PATH = "/mcp/"
JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

#: 三种非法形状：空列表、空串、纯空白。后两种在 JSON 里都是「有值但没内容」，
#: 正是 MCP 旧签名最容易放进链路的那一类。
MALFORMED_PAYLOADS: tuple[dict[str, Any], ...] = (
    {"kb_ids": [], "query": "hello"},
    {"kb_ids": ["kb-a"], "query": ""},
    {"kb_ids": ["kb-a"], "query": "   "},
)


class _Entries:
    """两条入口 + 可复用的 MCP 会话头；只暴露调用，不暴露应用内部。"""

    def __init__(self, client: TestClient, mcp_headers: dict[str, str]) -> None:
        self.client = client
        self.mcp_headers = mcp_headers

    def http_code(self, payload: dict[str, Any]) -> int:
        response = self.client.post(RETRIEVE_PATH, json=payload)
        assert response.status_code == 200
        return int(response.json()["code"])

    def mcp_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "rag_retrieve", "arguments": payload},
            },
            headers={**JSONRPC_HEADERS, **self.mcp_headers},
        )
        assert response.status_code == 200
        return dict(response.json()["result"])


def _mcp_code(result: dict[str, Any]) -> int:
    """MCP 的业务码藏在工具执行失败的文本里：``<code>|<msg>``（与既有双入口测试同形）。"""

    if not result.get("isError"):
        return int(ErrorCode.SUCCESS)
    text = result["content"][0]["text"]
    match = re.search(r"(\d{5})\|", text)
    assert match, f"MCP 失败结果里没有错误码：{text!r}"
    return int(match.group(1))


def _initialize_mcp(client: TestClient) -> dict[str, str]:
    response = client.post(
        MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "validation-parity", "version": "0"},
            },
        },
        headers=JSONRPC_HEADERS,
    )
    assert response.status_code == 200
    headers: dict[str, str] = {}
    session_id = response.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**JSONRPC_HEADERS, **headers},
    )
    return headers


@pytest.fixture
def entries(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Entries]:
    """HTTP 与 MCP 挂在同一个应用上；身份由同一份 ``KeyContext`` 给出。

    检索回调故意做成「被调用就炸」：本用例要证明非法请求在**进门校验**就被挡住，
    而不是先进入召回再因为别的原因失败。
    """

    main = importlib.import_module("app.api.router")
    error_handlers = importlib.import_module("app.api.error_handlers")
    mcp_server = importlib.import_module("app.mcp_server")
    from app.api.deps import get_current_key
    from tests import fake_db

    identity = KeyContext(tenant_id="tenant_parity", key_id="key-parity")

    async def must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("非法请求不该进入检索链路")

    async def fake_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _identity(*_args: Any, **_kwargs: Any) -> KeyContext:
        """MCP 侧的身份替身是**协程**（票 #130：判定内核是 ``async``）。"""

        return identity

    monkeypatch.setattr(mcp_server, "resolve_identity", _identity)

    session = RecordingSession()
    # MCP 子应用自己开会话（不在 FastAPI 依赖里），所以它拿到的是同一个假库的异步视图。
    view = AsyncSessionAdapter(session)
    mcp_app = mcp_server.create_mcp_http_app(
        # 检索链路用异步会话（第 11 步票 #127）。
        get_async_db=lambda: view,
        retrieve=must_not_run,
        list_kbs=fake_list,
    )
    app = FastAPI(lifespan=combine_lifespans(mcp_app.lifespan))
    app.include_router(main.api_router)
    error_handlers.install_handlers(app)
    app.mount("/mcp", mcp_app)
    fake_db.override_all_db(app, session)
    app.dependency_overrides[get_current_key] = lambda: identity

    try:
        with TestClient(app) as client:
            yield _Entries(client, _initialize_mcp(client))
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS)
def test_malformed_payloads_get_the_same_code_on_both_entries(
    entries: _Entries, payload: dict[str, Any]
) -> None:
    """同一份非法请求，REST 与 MCP 返回同一个业务码（``20002``），且都不进检索链路。"""

    http_code = entries.http_code(payload)
    mcp_code = _mcp_code(entries.mcp_result(payload))

    assert http_code == ErrorCode.REQUEST_VALIDATION_ERROR
    assert mcp_code == http_code, f"两条入口判出两种答案：HTTP={http_code} MCP={mcp_code}"
