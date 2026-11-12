"""配置中台的 HTTP 接口（``/console/settings``，票 #93 的后端部分）。

这一层只做「取参数、调服务、装壳」：白名单、档位、逐键校验、合并、影响预览与写入顺序
全在 :mod:`app.services.runtime_settings` 里。路由里出现 ``if spec.tier == ...`` 的那一刻，
#96 的页面、下一个入口（CLI / MCP）就得再写一遍同样的规则。

**为什么这两条接口只给管理员（admin-only），而不是给「有租户身份的账号」**：

``runtime_settings`` 是**平台级、无租户归属**的配置。``RERANK_ENABLED`` 一关，
**所有租户**的 ``quality`` 档位都会从「重排」退化到「降级」；``LLM_MODEL`` 一改，
所有租户的下一次调用都换了模型。让任意一个租户账号能 PATCH 它，等于给每个租户一把
改别人服务质量的钥匙——这是**跨租户越权**，比「谁的权限粒度粗一点」严重得多。
所以这里用 ``get_console_admin`` / ``require_console_admin_write``（后者已内含
``require_console_admin`` + 防跨站头），并且**不再叠加租户判定**：平台管理员
（``tenant_id is None``）正是这份配置唯一合理的归属，叠加上去只会把唯一合法的写者
挡在 20011 门外。租户账号读/写这里一律 ``20011``。

另一条纪律：**``dry_run`` 默认 True**——忘记传这个参数时最坏结果是「只试算、没保存」，
与改套餐同一条纪律（``app/api/routes/console.py`` 里的注释是同一句）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import (
    get_console_admin,
    get_runtime_settings_cache,
    require_console_admin_write,
)
from app.core.console_auth import ConsoleIdentity
from app.core.runtime_settings_cache import CACHE_TTL_SECONDS, RuntimeSettingsCache
from app.db.async_session import get_async_db, get_async_transaction
from app.db.transaction import AsyncTransaction
from app.schemas.common import ApiResponse, ok
from app.schemas.console import ImpactGroupData
from app.schemas.console_settings import (
    SettingsImpactData,
    SettingsListData,
    SettingsPatchData,
    SettingsPatchRequest,
    SettingRowData,
    SettingTierGroupData,
)
from app.services import runtime_settings
from app.services.runtime_settings import SettingRowView, SettingsImpact, SettingsPatchResult

router = APIRouter(prefix="/console", tags=["console"])


def _row_data(row: SettingRowView) -> SettingRowData:
    """内部视图 → 响应行：逐字段点名，不 ``model_validate``（内部字段不该变成契约）。"""

    return SettingRowData(
        key=row.key,
        tier=row.tier,
        editable=row.editable,
        value=row.value,
        source=row.source,
        invalid=row.invalid,
        why=row.why,
        invalid_reason=row.invalid_reason,
    )


def _list_data(rows: list[SettingRowView]) -> SettingsListData:
    """按三档分组。组的顺序来自服务层的 :data:`~app.services.runtime_settings.TIERS`，
    页面因此不需要自己排序，也不会在某一档为空时丢掉那一组（空组照样出现）。
    """

    groups: list[SettingTierGroupData] = []
    for tier in runtime_settings.TIERS:
        group_rows = [row for row in rows if row.tier == tier]
        groups.append(
            SettingTierGroupData(
                tier=tier,
                title=runtime_settings.TIER_TITLES[tier],
                editable=tier != runtime_settings.TIER_LOCKED,
                settings=[_row_data(row) for row in group_rows],
            )
        )
    return SettingsListData(groups=groups, cache_ttl_seconds=CACHE_TTL_SECONDS)


def _impact_data(impact: SettingsImpact | None) -> SettingsImpactData | None:
    if impact is None:
        return None
    return SettingsImpactData(
        quality_degraded=ImpactGroupData(
            count=impact.quality_degraded.count,
            sample=list(impact.quality_degraded.sample),
        ),
        keyword_channel_lost=ImpactGroupData(
            count=impact.keyword_channel_lost.count,
            sample=list(impact.keyword_channel_lost.sample),
        ),
    )


def _patch_data(result: SettingsPatchResult) -> SettingsPatchData:
    return SettingsPatchData(
        dry_run=result.dry_run,
        updated=list(result.updated),
        hot=list(result.hot),
        restart=list(result.restart),
        before=result.before,
        after=result.after,
        impact=_impact_data(result.impact),
        note=result.note,
    )


@router.get(
    "/settings",
    response_model=ApiResponse[SettingsListData],
    summary="运行时配置的三档清单（含来源与非法行标记）",
)
async def list_settings(
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[SettingsListData]:
    """按三档列出白名单的每一行：有效值、来源（env / db）、是否非法、为什么在这一档。

    面板本身**也是管理员才能看的**：它把平台的全部运行时开关（含供应商地址、模型名）
    摆在一页上，本身不是给租户用的视图。

    读的是库里的真相（不经过缓存）：这一页要回答「库里现在是什么」，而缓存最多比库晚
    30 秒；刚保存完就刷新却看到旧来源标记，会让人以为保存没成功。

    只读，因此整条路径在异步栈上（票 #129）：PATCH 仍在同步栈上（归 #130）。
    """

    rows = await runtime_settings.describe_settings(db)
    return ok(_list_data(rows))


@router.patch(
    "/settings",
    response_model=ApiResponse[SettingsPatchData],
    summary="改运行时配置（先试算、后落库；dry_run 默认 true）",
)
async def patch_settings(
    payload: SettingsPatchRequest,
    dry_run: bool = Query(default=True, description="true 只试算，false 才落库"),
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
    cache: RuntimeSettingsCache = Depends(get_runtime_settings_cache),
) -> ApiResponse[SettingsPatchData]:
    """配置的**唯一**写入口：白名单与档位 → 逐键校验 → 键间校验 → 落库 + 审计 → 删缓存。

    **只给管理员**：一次修改会横跨所有租户（关掉重排 = 所有租户的 quality 档位退化），
    所以它不能是「任意租户都能调的平台级写口」。``require_console_admin_write`` 已含
    管理员角色与防跨站头；这里**不再叠加租户判定**——平台管理员没有租户，正是这份配置
    唯一合理的归属。

    白名单外的键或 LOCKED 档一律 ``20015``（且整批不写）；``dry_run=true`` 只试算、
    不落库、不删缓存，返回 ``impact``。
    """

    result = await runtime_settings.patch_runtime_settings(
        db,
        values=payload.values,
        dry_run=dry_run,
        actor=identity,
        cache=cache,
    )
    return ok(_patch_data(result))
