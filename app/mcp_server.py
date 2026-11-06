"""MCP 入口（第 1 步接通）：两个工具共用 HTTP 侧同一批 Service。

- ``rag_retrieve``：入参 ``kb_ids``（列表）+ ``query``。
- ``rag_list_knowledge_bases``：无参，列这把 Key 可见的库。

占位鉴权与 HTTP 侧共用 ``resolve_dev_key``：从 JSON-RPC 请求上下文里取出
HTTP Request，校验头部 ``Authorization: Bearer dev-key``，解出默认租户。
第 5 步会把这里换成 ``api_keys`` 表与 ``allowed_kb_ids`` 白名单；
第 9 步再挂第三个工具 ``rag_research``。

本项目没有流式响应，``json_response=True`` 让工具调用回普通 JSON。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

from app.core.auth import resolve_dev_key
from app.core.exceptions import AppError


def _key_context() -> Any:
    """从 MCP 的 HTTP 请求上下文取占位 Key；不走真实鉴权。"""

    return resolve_dev_key(get_http_request())


def create_mcp_server(
    *,
    get_db: Callable,
    retrieve: Callable,
    list_kbs: Callable,
) -> FastMCP:
    """把两个工具挂到一个 FastMCP 实例上；业务规则全在回调里。"""

    mcp = FastMCP("rag-core")

    @mcp.tool(name="rag_retrieve")
    async def rag_retrieve(kb_ids: list[str], query: str) -> dict:
        """在指定知识库里检索切片（不生成答案）。"""

        key = _key_context()
        db = get_db()
        try:
            return await retrieve(db, tenant_id=key.tenant_id, kb_ids=kb_ids, query=query)
        except AppError as exc:
            # 业务失败不是协议错误：按 JSON-RPC 结果表达，文本里带 code。
            raise ValueError(f"{exc.code.value}|{exc.msg}") from exc
        finally:
            db.close()

    @mcp.tool(name="rag_list_knowledge_bases")
    async def rag_list_knowledge_bases() -> dict:
        """列这把 Key 可见的知识库（与 HTTP 的 GET /api/v1/knowledge-bases 对称）。"""

        key = _key_context()
        db = get_db()
        try:
            rows = list_kbs(db, key.tenant_id)
            return {
                "knowledge_bases": [
                    {"kb_id": row.id, "name": row.name, "tenant_id": row.tenant_id} for row in rows
                ]
            }
        finally:
            db.close()

    return mcp


def create_mcp_http_app(*, get_db: Callable, retrieve: Callable, list_kbs: Callable):
    """挂成子应用；path=\"/\" 让外层 mount 成为唯一的前缀来源。"""

    mcp = create_mcp_server(get_db=get_db, retrieve=retrieve, list_kbs=list_kbs)
    return mcp.http_app(path="/", transport="streamable-http", json_response=True)
