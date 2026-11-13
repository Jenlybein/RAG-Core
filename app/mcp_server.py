"""MCP 入口：三个工具共用 HTTP 侧同一批 Service 与同一套关卡。

- ``rag_retrieve``：入参 ``kb_ids``（列表）+ ``query``。
- ``rag_list_knowledge_bases``：无参，列这把 Key 可见的库。
- ``rag_research``：入参 ``kb_ids``（列表）+ ``query``，与 ``POST /api/v1/rag/research``
  同一份实现（票 #151）。

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

``rag_research`` 走的是同一形状的第二份门禁：``app.services.research_gate.gated_research``
（白名单 → 能力位 → 限流 → 日配额 → 范围解析 → 身份 → 状态机 → 归属索引 → 记账）。它
**和 ``gated_retrieve`` 一样被直接调用、不经过任何回调注入**——因为两者在 HTTP 侧也都是
模块级函数，``app/api/routes/rag.py`` 与本文件 import 的是同一个对象。工具因此连
「装配」都没有：它不构造检索内核、不选模型角色、不读套餐，只把身份、参数、会话递进去
（票 #151 的 AC：工具函数里没有任何业务判定）。反过来说，``retrieve`` 之所以是注入的，
是因为那里要传的是 ``rag_service.retrieve`` 这个**方法**；Research 没有这层包装。

**一个会话**（第 11 步票 #130）：身份判定与检索链路都在异步栈上，所以工具只开一个
``AsyncSession``，与 HTTP 侧完全同形——那边 ``get_current_key`` 与路由参数拿到的是
**同一个** ``get_async_db``（FastAPI 的依赖缓存），这里就显式用同一个会话对象。

业务失败（如 20008 / 20013 / 20005 / 20014）不是协议错误：按工具执行失败表达，文本里带
错误码。参数本身不合法（空列表、空串、纯空白、多给字段）另有一条路：多给或漏掉的是
**调用参数**，那是工具参数校验的事，连这个函数体都进不来；能进来的类型合法但语义非法的
值（空列表）走的是与 HTTP 同一个值对象，于是也拿到同一个 ``20002``。

本项目没有流式响应，``json_response=True`` 让工具调用回普通 JSON。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from pydantic import Field

from app.core.auth import KeyContext, resolve_identity
from app.core.exceptions import AppError
from app.schemas.rag import parse_retrieve_request
from app.schemas.research import parse_research_request
from app.services.data_scope import scoped_kb_ids
from app.services.research_gate import gated_research
from app.services.retrieval_gate import gated_retrieve


async def _key_context(db: Any) -> KeyContext:
    """MCP 侧的薄适配器：从请求上下文取 HTTP Request，交给判定内核。

    判定内核自票 #130 起是 ``async``（套餐缓存与库读都在事件循环上），所以这里只差
    「从哪拿请求」——不再需要线程池，也不再需要跨回主循环的过渡桥。
    """

    return await resolve_identity(get_http_request(), db)


def create_mcp_server(
    *,
    get_async_db: Callable,
    retrieve: Callable,
    list_kbs: Callable,
) -> FastMCP:
    """把三个工具挂到一个 FastMCP 实例上；业务规则全在回调里。

    ``get_async_db`` 是唯一一个会话工厂：判定内核与检索链路都在异步栈上（第 11 步
    票 #127 / #130），所以这里不再有「两个会话各管一段」的分工——那正是迁移期的形状。
    ``list_kbs`` 也是异步的（控制面与数据面列库现在都读 ``AsyncSession``）。
    """

    mcp = FastMCP("rag-core")

    @mcp.tool(name="rag_retrieve")
    async def rag_retrieve(kb_ids: list[str], query: str) -> dict:
        """在指定知识库里检索切片（不生成答案）。"""

        db = get_async_db()
        try:
            # 先取会话再判身份：真实 Key 的租户与套餐都在库里，判定本身要读它。
            key = await _key_context(db)
            # 与 HTTP 侧**同一个值对象**：工具签名只有类型，挡不住空列表与纯空白，所以
            # 这里显式走一遍 RetrieveRequest；失败是 20002，与 REST 同一个码。
            payload = parse_retrieve_request(kb_ids=kb_ids, query=query)
            # 白名单 → 档位门禁 → 配额 → 召回 → 记账，全部在同一个共享函数里
            # （与 HTTP 侧同一份实现、同一个码）：两条入口因此不可能判出两种答案。
            # ``retrieve`` 是既有的注入点（app.main 传 rag_service.retrieve），
            # 这里原样递进去，共享函数自己不知道调用方是谁。
            result = await gated_retrieve(
                db,
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
            await db.close()

    @mcp.tool(name="rag_research")
    async def rag_research(
        kb_ids: Annotated[
            list[str],
            Field(description="要查的知识库 id 列表，与 rag_retrieve 同一套白名单。"),
        ],
        query: Annotated[
            str,
            Field(
                description=(
                    "完整的自然语言问题。普通问题请先用 rag_retrieve：它只检索、不生成"
                    "答案，几百毫秒返回；这个工具要跑完五步、约 30 秒。"
                )
            ),
        ],
    ) -> dict:
        """一次受控 Research：多轮检索后给出引用块与完整材料（约 30 秒，有额外模型成本）。

        固定五步：规划 → 并行检索 → 判断 → 补证 → 生成引用块。耗时约 30 秒（典型值），
        最多 4 次模型调用，走一本独立的 Research 配额；**额外模型成本**由调用方承担。
        回答只能引用材料：``citations`` 的每块带证据下标（``E1`` 起），材料不足时它明确
        说「不足」而不是编内容；``answer = null`` 时材料、计划与停止原因照给。

        **普通问题先用 ``rag_retrieve``**：那个工具不生成答案、几百毫秒返回，绝大多数
        「找一段材料」的调用属于它，不属于这里。请求只有 ``kb_ids`` + ``query``：条数、
        轮次、档位一概不接受（与 HTTP 的 ``POST /api/v1/rag/research`` 同一个实现）。
        """

        db = get_async_db()
        try:
            # 先取会话再判身份：真实 Key 的租户与套餐都在库里，判定本身要读它。
            key = await _key_context(db)
            # 与 HTTP 侧**同一个值对象**（ResearchRequest 就是 RetrieveRequest）：工具
            # 签名只有类型，挡不住空列表与纯空白，所以显式走一遍；失败是 20002。
            payload = parse_research_request(kb_ids=kb_ids, query=query)
            # 11 步门禁 + 五步状态机 + 归属索引 + 记账，全部在同一个共享函数里（与 HTTP
            # 的 POST /api/v1/rag/research 同一份实现）：白名单、套餐、配额、档位一条都
            # 不在这个函数体里。
            data = await gated_research(
                db,
                key=key,
                kb_ids=payload.kb_ids,
                query=payload.query,
            )
            return data.model_dump()
        except AppError as exc:
            # 业务失败不是协议错误：按 JSON-RPC 结果表达，文本里带 code。四个码（20008 /
            # 20013 / 20005 / 20014）原样转发——它们是四个不同的问题，对应四种不同的
            # 处置，工具在这里合并或改码就等于替调用方决定了重试策略。
            raise ValueError(f"{exc.code.value}|{exc.msg}") from exc
        finally:
            await db.close()

    @mcp.tool(name="rag_list_knowledge_bases")
    async def rag_list_knowledge_bases() -> dict:
        """列这把 Key 可见的知识库（与 HTTP 的 GET /api/v1/knowledge-bases 对称）。"""

        db = get_async_db()
        try:
            key = await _key_context(db)
            rows = await list_kbs(db, key.tenant_id)
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
            await db.close()

    return mcp


def create_mcp_http_app(
    *, get_async_db: Callable, retrieve: Callable, list_kbs: Callable
):
    """挂成子应用；path="/" 让外层 mount 成为唯一的前缀来源。"""

    mcp = create_mcp_server(
        get_async_db=get_async_db, retrieve=retrieve, list_kbs=list_kbs
    )
    return mcp.http_app(path="/", transport="streamable-http", json_response=True)
