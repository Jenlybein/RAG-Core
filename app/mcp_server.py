"""MCP 入口（第 1 步接通）：两个工具共用 HTTP 侧同一批 Service。

- ``rag_retrieve``：入参 ``kb_ids``（列表）+ ``query``。
- ``rag_list_knowledge_bases``：无参，列这把 Key 可见的库。

占位鉴权与 HTTP 侧共用同一份判定内核 ``app.core.auth.resolve_identity``：从 JSON-RPC
请求上下文里取出 HTTP Request，连同本次工具调用自己的数据库会话一起交进去。这里只做
「取请求 + 取会话」两件事——判定规则只有一份，两个入口不允许各自长出一套。
真实 Key、``allowed_kb_ids`` 白名单与套餐复核都发生在那份核心里（第 7 步）。
第 9 步再挂第三个工具 ``rag_research``。

本项目没有流式响应，``json_response=True`` 让工具调用回普通 JSON。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

from app.core.auth import resolve_identity
from app.core.exceptions import AppError


def _key_context(db: Any) -> Any:
    """MCP 侧的薄适配器：从请求上下文取 HTTP Request，交给判定内核。"""

    return resolve_identity(get_http_request(), db)


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

        db = get_db()
        try:
            # 先取会话再判身份：真实 Key 的租户与套餐都在库里，判定本身要读它。
            key = _key_context(db)
            return await retrieve(db, tenant_id=key.tenant_id, kb_ids=kb_ids, query=query)
        except AppError as exc:
            # 业务失败不是协议错误：按 JSON-RPC 结果表达，文本里带 code。
            raise ValueError(f"{exc.code.value}|{exc.msg}") from exc
        finally:
            db.close()

    @mcp.tool(name="rag_list_knowledge_bases")
    async def rag_list_knowledge_bases() -> dict:
        """列这把 Key 可见的知识库（与 HTTP 的 GET /api/v1/knowledge-bases 对称）。"""

        db = get_db()
        try:
            key = _key_context(db)
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
