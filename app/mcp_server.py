"""MCP 入口：两个工具共用 HTTP 侧同一批 Service 与同一套关卡。

- ``rag_retrieve``：入参 ``kb_ids``（列表）+ ``query``。
- ``rag_list_knowledge_bases``：无参，列这把 Key 可见的库。

**签名就是权限**：工具参数与 HTTP 请求体字面一致（都只有 ``kb_ids`` + ``query``），
``tenant_id`` / 套餐 / 档位 / 白名单 / 检索方式 / 返回条数 / 结果形状一律不出现——
签名里有什么，模型就能填什么（ADR-0018）。身份由 ``Authorization`` 头带进来，
JSON-RPC 信封本身不带任何授权信息。这次调用「用多好」由密钥配置解析成
``RetrievalOptions``（``app.services.retrieval_options``），与 HTTP 侧同一个函数。

判定只有一份：这里从 JSON-RPC 请求上下文里取出 HTTP Request，连同本次工具调用自己的
数据库会话一起交给判定内核 ``app.core.auth.resolve_identity``；随后把身份、参数与会话
递进**同一个**门禁化检索 ``app.services.retrieval_gate.gated_retrieve``（HTTP 侧的路由
调的也是它，白名单 / 档位门禁 / 配额 / 召回 / 记账的顺序都封在里面）。这里只做「取请求
+ 取会话 + 递参数」三件事，不判规则，也不各长一套。

**两个会话**（第 11 步票 #127）：身份判定用同步会话，检索链路用异步会话——HTTP 侧也是
这么分的（``get_db`` 给 ``get_current_key``，``get_async_db`` 给检索）。两条入口共用同一个
门禁化检索函数，所以「哪一步该用哪个会话」在两侧是同一份决定。

业务失败（如 20008 / 20010）不是协议错误：按工具执行失败表达，文本里带错误码。第 9 步
再挂第三个工具 ``rag_research``。

本项目没有流式响应，``json_response=True`` 让工具调用回普通 JSON。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

from app.core.auth import KeyContext, resolve_identity
from app.core.exceptions import AppError
from app.schemas.rag import parse_retrieve_request
from app.services.data_scope import scoped_kb_ids
from app.services.retrieval_gate import gated_retrieve


def _key_context(db: Any) -> KeyContext:
    """MCP 侧的薄适配器：从请求上下文取 HTTP Request，交给判定内核。"""

    return resolve_identity(get_http_request(), db)


async def _key_context_async(db: Any) -> KeyContext:
    """同上，但在**线程池**里跑判定内核（票 #129 起必须如此）。

    判定内核是同步函数，并且它要解析套餐——套餐缓存自 #129 起是 ``redis.asyncio``，
    同步上下文里的那一段由 :func:`app.core.sync_bridge.run_blocking` 交回主事件循环执行。
    那个桥要求「调用它的线程不是事件循环线程」，而 MCP 工具本身是 ``async def``：
    直接在工具里调内核会撞上「循环线程上不许阻塞等待」的守卫。

    所以这里显式跳一次线程池（与 FastAPI 跑同步依赖用的是同一套机制）：内核的同步库读
    因此**离开事件循环**，套餐解析又能回到主循环上 ``await`` 同一个异步 Redis 客户端。
    """

    return await run_in_threadpool(_key_context, db)


def create_mcp_server(
    *,
    get_db: Callable,
    get_async_db: Callable,
    retrieve: Callable,
    list_kbs: Callable,
) -> FastMCP:
    """把两个工具挂到一个 FastMCP 实例上；业务规则全在回调里。

    两个会话工厂：``get_db``（同步）给身份判定，``get_async_db``（异步）给检索链路
    （第 11 步票 #127）。它们**刻意分开**——判定内核还在同步栈上，而检索已经搬走了；
    合并成一个只会把「这次该不该 await」变成运行期的事。
    """

    mcp = FastMCP("rag-core")

    @mcp.tool(name="rag_retrieve")
    async def rag_retrieve(kb_ids: list[str], query: str) -> dict:
        """在指定知识库里检索切片（不生成答案）。"""

        db = get_db()
        retrieval_db = get_async_db()
        try:
            # 先取会话再判身份：真实 Key 的租户与套餐都在库里，判定本身要读它。
            key = await _key_context_async(db)
            # 与 HTTP 侧**同一个值对象**：工具签名只有类型，挡不住空列表与纯空白，所以
            # 这里显式走一遍 RetrieveRequest；失败是 20002，与 REST 同一个码。
            payload = parse_retrieve_request(kb_ids=kb_ids, query=query)
            # 白名单 → 档位门禁 → 配额 → 召回 → 记账，全部在同一个共享函数里
            # （与 HTTP 侧同一份实现、同一个码）：两条入口因此不可能判出两种答案。
            # ``retrieve`` 是既有的注入点（app.main 传 rag_service.retrieve），
            # 这里原样递进去，共享函数自己不知道调用方是谁。
            result = await gated_retrieve(
                retrieval_db,
                key=key,
                kb_ids=payload.kb_ids,
                query=payload.query,
                retrieve=retrieve,
            )
            # 与 HTTP 侧同一个时点：业务跑完、响应也校验成形之后才记账（在共享函数里）。
            return result.model_dump()
        except AppError as exc:
            # 业务失败不是协议错误：按 JSON-RPC 结果表达，文本里带 code。
            raise ValueError(f"{exc.code.value}|{exc.msg}") from exc
        finally:
            await retrieval_db.close()
            db.close()

    @mcp.tool(name="rag_list_knowledge_bases")
    async def rag_list_knowledge_bases() -> dict:
        """列这把 Key 可见的知识库（与 HTTP 的 GET /api/v1/knowledge-bases 对称）。"""

        db = get_db()
        try:
            key = await _key_context_async(db)
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


def create_mcp_http_app(
    *, get_db: Callable, get_async_db: Callable, retrieve: Callable, list_kbs: Callable
):
    """挂成子应用；path="/" 让外层 mount 成为唯一的前缀来源。"""

    mcp = create_mcp_server(
        get_db=get_db, get_async_db=get_async_db, retrieve=retrieve, list_kbs=list_kbs
    )
    return mcp.http_app(path="/", transport="streamable-http", json_response=True)

