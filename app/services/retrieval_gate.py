"""整段门禁化的检索：白名单 → 档位门禁 → 配额 → 召回 → 成功记账（票 #76 / #78 / #79）。

HTTP 路由与 MCP 工具写的是**同一段五步序列**，过去是两处各写一遍、注释都说「同一份实现」。
两份拷贝的第六道关卡意味着改两处，而「改了一处、另一处忘了」正是这类判定最坏的失败方式：
两条入口从此判出不同答案，测试还恰好只覆盖一边。本模块是那段序列的**唯一实现**，
两个入口只负责把身份、参数和会话递进来，再把同一个 :class:`RetrieveData` 装成各自的形状。

顺序即判定的一部分（ADR-0018 第 4 条），后一步依赖前一步的产出：

```text
1 白名单      kb_ids ⊆ allowed_kb_ids            → 20008
2 套餐复核    + 档位展开与门禁 + 单次库数       → 20013
3 配额        秒级限流 → 日配额                  → 20005 / 20014
4 召回        交给组合根 rag_service.retrieve
5 记账        业务成功、响应也成形之后才 +1
```

**记账时点刻意在最后**：配额「进门只查、成功后记」（ADR-0018），所以被拒的请求一次都不
扣额度。``RetrieveData`` 的校验也放在记账之前——响应形状不成立就不算一次成功调用。

失败一律抛 :class:`AppError`：越权与超限是业务失败（ADR-0007），不是协议错误。两个入口
对它的处理不同（HTTP 交给异常处理器变成 ``{code, msg}`` 外壳，MCP 转成 JSON-RPC 的
``isError`` 文本），但**抛什么、什么时候抛**只有这一份。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import KeyContext
from app.schemas.rag import RetrieveData
from app.services import rag_service
from app.services.data_scope import ensure_kb_scope
from app.services.profile_policy import resolve_retrieval_options
from app.services.quota_gate import retrieval_quota

#: ``rag_service.retrieve`` 的形状：组合根只认「一个会话 + 已解析参数」。
#: MCP 的 ``create_mcp_server`` 把它做成可注入的回调（既有接缝，测试与 app.main 都在用），
#: 所以这里也允许显式传入；不传就取模块属性，monkeypatch 替换 ``rag_service.retrieve``
#: 之后两个入口仍然一起换掉。
RetrieveCallable = Callable[..., Awaitable[dict[str, Any]]]


async def gated_retrieve(
    db: AsyncSession,
    *,
    key: KeyContext,
    kb_ids: list[str],
    query: str,
    retrieve: RetrieveCallable | None = None,
) -> RetrieveData:
    """按六道关卡的顺序跑完整次检索，返回**已经校验成形**的响应数据。

    ``db`` 是**请求级异步会话**（第 11 步票 #127）：第 1~3 步不碰库，第 4 步把它原样交给
    组合根。身份判定仍然走同步会话——那是 ``get_current_key`` 自己的依赖，不在本函数里。

    ``key`` 是判定内核的结论（租户、套餐、密钥配置都在上面）。``kb_ids`` / ``query`` 是
    请求的全部内容——质量参数由 ``profile_policy`` 从密钥配置解析，调用方看不到。

    参数非法、越权、超限、配额用尽都在这里抛出 ``AppError``；业务异常原样上抛。调用方
    不再补任何判定，也不许在调用之后再判一次「这一步该不该做」。
    """

    retriever: RetrieveCallable = rag_service.retrieve if retrieve is None else retrieve

    # 第 1 步：白名单。身份已经解出来（``key`` 就在手上），限流、配额与召回都还没开始——
    # 顺序反了会让越权请求先扣掉客户当天的额度（ADR-0018「六道关卡」）。
    ensure_kb_scope(policy=key.policy, kb_ids=kb_ids)

    # 第 2 步：套餐复核 + 档位展开与门禁 + 单次库数。越权在这里就失败，一次嵌入都不该烧。
    options = resolve_retrieval_options(
        policy=key.policy,
        plan=key.plan,
        kb_count=len(kb_ids),
    )

    # 第 3 步：秒级限流 → 日配额。**进门只查**，成功后记账由第 5 步负责。开关关着时这本账
    # 是 no-op，默认套件因此不需要 Redis。计数命令是异步的（票 #133）：它们在这条异步
    # 链路上，同步 Redis 会把整个事件循环按住。
    quota = retrieval_quota(
        policy=key.policy,
        plan=key.plan,
        tenant_id=key.tenant_id,
        key_id=key.key_id,
    )
    await quota.ensure_available()

    # 第 4 步：召回。组合根拿到的是已经解析完的参数，它不再读请求体、也不再读请求里的质量
    # 字段（那些字段根本不存在）。
    payload = await retriever(
        db,
        tenant_id=key.tenant_id,
        kb_ids=kb_ids,
        query=query,
        options=options,
    )

    # 第 5 步：响应成形之后才 +1。校验失败的响应不是一次成功调用，所以顺序不能倒过来；
    # 失败 / 异常 / 校验不通过的路径都不会走到这里（ADR-0018「成功后才计数」）。
    result = RetrieveData.model_validate(payload)
    await quota.record_success()
    return result
