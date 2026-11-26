"""管理端「观测总览」接口（三条，全部只读）。

三条都在**管理员**门禁下（``get_console_admin``），与账号 / 套餐 / 审计同一道门：观测数据是
平台级资源，它跨租户，因此不属于任何一个租户的身份。

三条边界：

- **只读**：这里没有任何写操作。要改观测配置去改部署（`.env`），不在控制台里改——
  一个能在线关掉监控的开关，本身就是一个监控盲区。
- **密钥永不出服务端**：`stack` 只回布尔与对外地址。`LANGFUSE_SECRET_KEY` 能读整栈，
  进浏览器就等于公开。
- **观测栈不可用时不 500**：`stages` 会返回 `source=none` 与原因，页面据此显示「未接入 /
  不可达」。监控页面不应该因为被监控的系统挂了而整页报错。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import get_console_admin
from app.core.console_auth import ConsoleIdentity
from app.db.async_session import get_async_db
from app.schemas.common import ApiResponse, ok
from app.services import admin_observability

router = APIRouter(prefix="/console/admin", tags=["console"])


@router.get(
    "/observability/overview",
    response_model=ApiResponse[dict],
    summary="观测总览（本地事实：量、耗时、降级、评分、租户榜、趋势）",
)
async def observability_overview(
    hours: int = Query(24, description="窗口小时数，取值 1/6/24/72/168；非法值收敛到 24"),
    scope: str = Query(
        "all",
        description="统计范围：all（含被拒）/ ok（只算 code=0）/ failed（只算 code<>0）",
    ),
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """跨租户的检索量与质量概况。

    数据源是**我们自己的库**（`usage_events` / `retrieval_feedback`），所以观测栈挂了
    这里照样有数——这正是这张页面存在的意义：栈挂的那一刻，最需要的恰恰是「最后一次成功
    的请求是几点」。

    `scope` 是三段式的第 14 步补充：被拒的请求（`20014` 等）现在也落行，于是「全部」
    不再是「全部成功」。降级率与空结果率只在 `ok` 那一档里有意义（响应里的
    `rates_meaningful` 会说这件事）。
    """

    return ok(await admin_observability.overview(db, hours=hours, scope=scope))


@router.get(
    "/observability/stages",
    response_model=ApiResponse[dict],
    summary="六段耗时聚合（来自观测栈；取不到时 source=none，不画 0）",
)
def observability_stages(
    hours: int = Query(24, description="窗口小时数，取值 1/6/24/72/168；非法值收敛到 24"),
    identity: ConsoleIdentity = Depends(get_console_admin),
) -> ApiResponse[dict]:
    """六段耗时的分位数与占比。

    ``source`` 会如实标注数据从哪来：``langfuse`` 表示来自观测栈，``none`` 表示这次取不到
    （没接栈 / 凭据不全 / 栈不可达）。**「取不到」不画成 0** —— 一屏 0 毫秒的瀑布图会让
    人以为检索变快了。
    """

    return ok(admin_observability.stage_stats(hours=hours))


@router.get(
    "/observability/stack",
    response_model=ApiResponse[dict],
    summary="观测栈状态（只回布尔与地址，不回密钥）",
)
def observability_stack(
    identity: ConsoleIdentity = Depends(get_console_admin),
) -> ApiResponse[dict]:
    """观测栈现在接没接、齐不齐。用来回答「为什么这一屏是空的」。

    只回三件事：去向（`sink`）、凭据是否齐全（`has_keys`）、对外地址（`langfuse_host`）。
    私钥一个字都不到浏览器——即使只是「看一眼」也不该下发。
    """

    return ok(admin_observability.stack_status())
