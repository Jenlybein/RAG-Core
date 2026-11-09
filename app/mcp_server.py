"""MCP 入口：两个工具共用 HTTP 侧同一批 Service 与同一套关卡。

- ``rag_retrieve``：入参 ``kb_ids``（列表）+ ``query``。
- ``rag_list_knowledge_bases``：无参，列这把 Key 可见的库。

**签名就是权限**：工具参数与 HTTP 请求体字面一致（都只有 ``kb_ids`` + ``query``），
``tenant_id`` / 套餐 / 档位 / 白名单 / 检索方式 / 返回条数 / 结果形状一律不出现——
签名里有什么，模型就能填什么（ADR-0018）。身份由 ``Authorization`` 头带进来，
JSON-RPC 信封本身不带任何授权信息。这次调用「用多好」由密钥配置解析成
``RetrievalOptions``（``app.services.retrieval_options``），与 HTTP 侧同一个函数。

判定只有一份：这里从 JSON-RPC 请求上下文里取出 HTTP Request，连同本次工具调用自己的
数据库会话一起交给判定内核 ``app.core.auth.resolve_identity``；随后按六道关卡的顺序调
**同一个**数据范围关卡 ``app.services.data_scope.ensure_kb_scope`` 与**同一个**档位门禁
``app.services.tier_policy.resolve_retrieval_options``（HTTP 侧的路由调的是同一对函数）。
这里只做「取请求 + 取会话 + 递参数」三件事，不判规则，也不各长一套。

业务失败（如 20008 / 20010）不是协议错误：按工具执行失败表达，文本里带错误码。第 9 步
再挂第三个工具 ``rag_research``。

本项目没有流式响应，``json_response=True`` 让工具调用回普通 JSON。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

from app.core.auth import KeyContext, resolve_identity
from app.core.exceptions import AppError
from app.schemas.rag import RetrieveData
from app.services.data_scope import ensure_kb_scope, scoped_kb_ids
from app.services.quota_gate import retrieval_quota
from app.services.tier_policy import resolve_retrieval_options


def _key_context(db: Any) -> KeyContext:
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
            # 第二道关卡与 HTTP 侧同一份实现、同一个码：白名单外与跨租户都回「不存在」。
            ensure_kb_scope(policy=key.policy, kb_ids=kb_ids)
            # 第三 / 第四道关卡也是同一份实现（套餐复核 + 档位门禁 + 单次库数）：
            # 「这次用多好」不是请求能表达的，两条入口因此也不可能判出两种答案。
            options = resolve_retrieval_options(
                policy=key.policy, plan=key.plan, kb_count=len(kb_ids)
            )
            # 第五 / 六道关卡与 HTTP 侧同一份实现：进门查、成功记，两个错误码也一样。
            quota = retrieval_quota(
                policy=key.policy,
                plan=key.plan,
                tenant_id=key.tenant_id,
                key_id=key.key_id,
            )
            quota.ensure_available()
            payload = await retrieve(
                db,
                tenant_id=key.tenant_id,
                kb_ids=kb_ids,
                query=query,
                options=options,
            )
            # 与 HTTP 侧同一个时点：业务跑完且响应成形之后才记账。
            result = RetrieveData.model_validate(payload).model_dump()
            quota.record_success()
            return result
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
            # 与 HTTP 列库同一套收窄：清单本身就是「你能看哪些库」的答案。
            visible = set(scoped_kb_ids(policy=key.policy, kb_ids=[row.id for row in rows]))
            return {
                "knowledge_bases": [
                    {"kb_id": row.id, "name": row.name, "tenant_id": row.tenant_id}
                    for row in rows
                    if row.id in visible
                ]
            }
        finally:
            db.close()

    return mcp


def create_mcp_http_app(*, get_db: Callable, retrieve: Callable, list_kbs: Callable):
    """挂成子应用；path=\"/\" 让外层 mount 成为唯一的前缀来源。"""

    mcp = create_mcp_server(get_db=get_db, retrieve=retrieve, list_kbs=list_kbs)
    return mcp.http_app(path="/", transport="streamable-http", json_response=True)
