"""用量看板接口（第 14 步票 #202）。

两条边界，与观测页同一套：

- **租户那条只认会话身份**：`tenant_id` 不从 query 参数来（那是「查别人用量」的后门）；
  带 `key_id` 时必须先确认它属于这个租户，否则 `20011`。
- **管理员那条跨租户**：`get_console_admin` 是同一道门；响应里带 `tenant_id` 是它的本分。

返回的都是 `ApiResponse[dict]`（与观测页一致）：形状在服务层的 docstring 里写清，
前端在 `types/usage.ts` 里对着它写类型。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import get_console_admin, require_console_tenant
from app.core.console_auth import ConsoleIdentity
from app.core.exceptions import AppError, ErrorCode
from app.db.async_session import get_async_db
from app.repositories import api_key_repository
from app.schemas.common import ApiResponse, ok
from app.services import console_usage

#: 租户侧：`/console/usage`。
router = APIRouter(prefix="/console", tags=["console"])

#: 平台侧：`/console/admin/usage/*`。
admin_router = APIRouter(prefix="/console/admin", tags=["console"])

WINDOW_DOC = "时间窗：1h / 1d / 7d / 30d；非法值收敛到 1d"


@router.get(
    "/usage",
    response_model=ApiResponse[dict],
    summary="我的用量（可按 Key 收窄；趋势 + 合计 + 按 Key 拆分）",
)
async def tenant_usage(
    window: str = Query("1d", description=WINDOW_DOC),
    key_id: str | None = Query(None, description="只看这一把 Key（必须属于本租户）"),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """本租户的用量：一条时间序列 + 合计 + 每把 Key 的拆分。

    为什么要先校验 `key_id` 的归属：`key_id` 来自前端，而它不是身份的一部分。不校验的话，
    猜一个别人的 key_id 就能读到别人的曲线——那是一条**读**后门，比写还难发现。
    """

    tenant_id = identity.tenant_id
    if not tenant_id:
        # ``require_console_tenant`` 已经挡过一次；这里再挡一次是因为类型上它是 ``str | None``，
        # 而下面每一处 SQL 都拿它当过滤条件——漏一次就是跨租户。
        raise AppError(ErrorCode.FORBIDDEN, "tenant account required")

    if key_id:
        key = await api_key_repository.get_by_id(db, key_id)
        if key is None or key.tenant_id != tenant_id:
            # 与「不存在」同一个答案：不告诉调用方「这把 Key 存在，但不是你的」。
            raise AppError(ErrorCode.FORBIDDEN, "api key does not belong to this tenant")

    parsed = console_usage.parse_window(window)
    detail = await console_usage.series(db, window=parsed, tenant_id=tenant_id, key_id=key_id)
    return ok(
        {
            **detail,
            # 不带 key_id 时才需要「按 Key 拆分」；带了的话它就是单 Key 视图。
            "keys": []
            if key_id
            else await console_usage.keys_of_tenant(db, tenant_id=tenant_id, window=parsed),
        }
    )


@admin_router.get(
    "/usage/site",
    response_model=ApiResponse[dict],
    summary="站点用量（全站趋势 + 合计 + 需要看一眼）",
)
async def site_usage(
    window: str = Query("1d", description=WINDOW_DOC),
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """全站用量。`alerts` 里的三条提示按运行时配置的阈值判，改阈值不需要重起。"""

    parsed = console_usage.parse_window(window)
    detail = await console_usage.series(db, window=parsed)
    return ok(
        {
            **detail,
            "alerts": await console_usage.alerts(db, window=parsed, site=detail),
            "keys": [],
        }
    )


@admin_router.get(
    "/usage/tenants",
    response_model=ApiResponse[dict],
    summary="按租户排的用量榜（点进去就是那一个租户）",
)
async def tenants_usage(
    window: str = Query("1d", description=WINDOW_DOC),
    limit: int = Query(20, ge=1, le=100, description="最多几个租户"),
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """哪个租户用得最多、谁在出问题。只读，按调用量倒序。"""

    parsed = console_usage.parse_window(window)
    return ok(
        {
            "window": parsed.name,
            "source": "daily" if parsed.uses_daily else "events",
            "tenants": await console_usage.tenants(db, window=parsed, limit=limit),
        }
    )


@admin_router.get(
    "/usage/tenants/{tenant_id}",
    response_model=ApiResponse[dict],
    summary="下钻到一个租户（趋势 + 合计 + 它的 Key）",
)
async def tenant_usage_detail(
    tenant_id: str,
    window: str = Query("1d", description=WINDOW_DOC),
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[dict]:
    """某个租户的用量。**不校验租户存不存在**：查一个不存在的租户得到全 0 的空图，
    比得到一个 404 更有用——它回答了「这个 id 到底有没有量」。
    """

    parsed = console_usage.parse_window(window)
    detail = await console_usage.series(db, window=parsed, tenant_id=tenant_id)
    return ok(
        {
            **detail,
            "tenant_id": tenant_id,
            "keys": await console_usage.keys_of_tenant(db, tenant_id=tenant_id, window=parsed),
        }
    )
