"""控制面「按 ``log_id`` 读回瀑布」接口（``/console/retrieval-traces/...``，#103）。

与 :mod:`app.api.routes.console`（登录 / 套餐 / 账号 / 审计）分成两个路由模块，各自
``include_router``：并行票共用同一个文件会让每一票的 diff 互相重叠；本模块只带
``prefix="/console"``，不重复那一条。

两条边界写在这里，因为它们都是「少一个参数」的事故：

- **走读门 ``require_console_tenant_async``**：会话 cookie + 租户账号。管理员（``tenant_id IS
  NULL``）一律 ``20011``——它不属于任何租户，而瀑布图恰恰是租户内的数据；不带防跨站头，
  因为看瀑布不改任何状态。（``/runtime-info`` 是唯一不碰数据库的一条，仍用同步那一份
  门禁——同一个判定，见 ``app/core/console_auth``。）
- **租户只来自会话**：路径里只有 ``log_id``，没有 ``tenant_id``，也没有「用哪把 Key」。
  归属校验在 :func:`app.services.retrieval_waterfall.waterfall_of` 里做，跨租户与不存在
  同为 ``20016``。

观测服务的读入口由 :func:`app.api.console_deps.get_langfuse_reader` 注入（测试的接缝），
密钥不进这一层，也就不可能进响应。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import (
    get_langfuse_reader,
    require_console_tenant,
    require_console_tenant_async,
    tenant_id_of,
)
from app.core.config import settings
from app.core.console_auth import ConsoleIdentity
from app.db.async_session import get_async_db
from app.repositories import retrieval_feedback_repository, retrieval_trace_repository
from app.schemas.common import ApiResponse, ok
from app.schemas.console_traces import RetrievalWaterfallData, waterfall_data
from app.services.retrieval_waterfall import RemoteStageReader, waterfall_of

router = APIRouter(prefix="/console", tags=["console"])

#: ``trace_id`` 的合法形状（32 位十六进制）。校验它不是洁癖：它决定「查不查库」。
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


@router.get(
    "/retrieval-traces/{log_id}/waterfall",
    response_model=ApiResponse[RetrievalWaterfallData],
    summary="按 log_id 读回一次检索的六段瀑布（先本地记录、后观测服务）",
)
async def retrieval_waterfall(
    log_id: str = Path(..., min_length=1, description="归属索引的行 id，即 log_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant_async),
    remote_reader: RemoteStageReader = Depends(get_langfuse_reader),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[RetrievalWaterfallData]:
    """六段名称与耗时；两处都没记上时返回结构化的「未启用 / 无记录」，不是 404。

    这里刻意**不**把「没记录」也做成 404：404 在这一层只有一个含义——这个 ``log_id``
    对当前租户不存在（跨租户同样如此）。而「存在、但观测没开着」是**正常状态**，
    前端要靠 ``enabled`` / ``reason`` 说出「没配」而不是画一张空卡片。

    只读，因此整条路径在异步栈上（票 #129）：归属行读异步会话，本地 JSONL 与观测服务
    两处同步 I/O 由服务层挪进线程池。
    """

    waterfall = await waterfall_of(
        db,
        log_id=log_id,
        tenant_id=tenant_id_of(identity),
        remote_reader=remote_reader,
    )
    return ok(waterfall_data(waterfall))


@router.get(
    "/retrieval-traces",
    response_model=ApiResponse[dict],
    summary="按 trace_id 反查 log_id（控制面，只读）",
)
async def retrieval_trace_by_trace_id(
    trace_id: str = Query(..., min_length=1, description="检索响应 metadata.trace_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant_async),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """把「这根线」翻回「那一行」，供反馈与瀑布使用（#106）。

    ``trace_id`` 是 **query 参数，不是路径参数**：这条路由的路径里没有 ``{trace_id}``
    占位符（上一条瀑布用的是 ``{log_id}/waterfall``），前端 ``consoleTraces.ts`` 也按
    query 传。声明成 ``Path`` 会让 FastAPI 把它登记成一个不存在的路径段，实际调用
    直接 20002 —— 这个错法离线也能全绿（测试若直接调函数就绕过了路由），所以这里
    写明，并由走 TestClient 的用例守着。

    **为什么加这条端点而不是往检索响应里塞 ``log_id``**：ADR-0010 第五次修订的红线是
    「除 ``metadata.trace_id`` 外响应逐字节不变」，加字段要再开一次修订并重采黄金哈希；
    而 ``trace_id`` 是 ``log_id`` 的**单向**派生，推不回去，于是反查只能放在控制面。
    这里只读、走会话身份，因此不碰数据面契约。

    非法形状**直接当不存在**：它不拼 SQL（按等值查），但仍然先挡一道，避免把「随便一串」
    当成一次查询去打库。
    """

    normalized = trace_id.strip().lower()
    if not _TRACE_ID.fullmatch(normalized):
        return ok({"log_id": None, "trace_id": normalized})

    row = await retrieval_trace_repository.get_by_trace_id_for_tenant(
        db, normalized, tenant_id=tenant_id_of(identity)
    )
    # 跨租户与不存在同一个答复：不泄漏「这条 trace 在别人那儿存在」。
    return ok({"log_id": row.id if row else None, "trace_id": normalized})


@router.get(
    "/retrieval-traces/recent",
    response_model=ApiResponse[dict],
    summary="最近若干次检索（控制台列表：时间 / 耗时 / 结局 / 是否已评分）",
)
async def recent_retrieval_traces(
    limit: int = Query(20, ge=1, le=100, description="返回条数上限"),
    identity: ConsoleIdentity = Depends(require_console_tenant_async),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """本租户最近若干条归属索引，按时间倒序。

    **为什么需要它**：瀑布、评分、旁证三条路都要先知道一个 ``log_id`` 或 ``trace_id``。
    没有列表时，使用者只能回到调用方系统里翻 id 再抄过来——那等于把「看一眼最近的检索」
    做成一件需要另一个系统配合的事。

    ``limit`` 有上限（100）：「最近几次」是给人看的，不是导出接口；没有上限的列表查询在
    表长大之后会变成一次全表扫。

    列表里**没有任何原文**：归属索引本来就不存（ADR-0021 决策 1），所以这里不需要额外脱敏。

    两次读（归属索引、评分）都在异步会话上（票 #129），顺序不变：先取行，再按这些
    ``log_id`` 批量取分数——分数只认本租户。
    """

    rows = await retrieval_trace_repository.list_recent_for_tenant(
        db, tenant_id=tenant_id_of(identity), limit=limit
    )
    scores = await retrieval_feedback_repository.scores_for_log_ids(
        db, [row.id for row in rows], tenant_id=tenant_id_of(identity)
    )
    return ok(
        {
            "items": [
                {
                    "log_id": row.id,
                    "trace_id": row.trace_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "latency_ms": row.latency_ms,
                    "top_k": row.top_k,
                    "kb_count": len(row.kb_ids or []),
                    "degraded_reason": row.degraded_reason,
                    "empty_reason": row.empty_reason,
                    # 未评分是 ``None``（不是 0）：列表页据此画「还没人评」，而不是画 0 星。
                    "score": scores.get(row.id),
                }
                for row in rows
            ],
            "limit": limit,
        }
    )


@router.get(
    "/runtime-info",
    response_model=ApiResponse[dict],
    summary="控制台需要知道的运行时事实（观测去向 / 观测栈地址 / 鉴权是否开启）",
)
def runtime_info(
    identity: ConsoleIdentity = Depends(require_console_tenant),
) -> ApiResponse[dict]:
    """把「这个服务现在是怎么配的」里，界面必须知道的那几项一次说清。

    三条都是**界面会据以改变行为**的事实，不是给排障看的全量配置：

    - ``trace_sink``：六段耗时往哪送。去向是 ``jsonl`` / ``none`` 时观测栈里没有这条 trace，
      「在观测栈里打开」的链接不该画出来（画了只会点出 404）。
    - ``langfuse_host``：观测栈 UI 的**对外**地址，只下发地址、不下发任何密钥。
    - ``auth_enabled``：数据面是否在「开发身份模式」。**关着的时候，控制台签发的 Key
      一律不参与鉴权**（只认内置占位 Key）。界面必须提前说清这件事——否则使用者会看到
      「Key 无效」，然后反复重签一把永远不会生效的 Key。

    ``identity`` 只用来套租户门禁（管理员无租户，对它 ``20011``）；这条端点本身不返回
    任何租户数据。
    """

    sink = settings.TRACE_SINK
    # 给浏览器用的是**对外地址**：容器里 `LANGFUSE_HOST` 可能是服务名（`langfuse-web`），
    # 浏览器解析不了。`LANGFUSE_PUBLIC_URL` 留空时才退回 `LANGFUSE_HOST`（单机部署两者相同）。
    host = (settings.LANGFUSE_PUBLIC_URL or settings.LANGFUSE_HOST).rstrip("/")
    return ok(
        {
            "trace_sink": sink,
            # 只有去向真的是 langfuse 且地址非空时才给地址，否则前端拿到的会是空串。
            "langfuse_host": host if (sink == "langfuse" and host) else None,
            "auth_enabled": settings.AUTH_ENABLED,
        }
    )