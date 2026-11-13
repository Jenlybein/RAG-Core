"""档位预设的 HTTP 接口（``/console/profile-presets``）。

**为什么只给管理员**：``profile_presets`` 是平台级、无租户归属的资源——改一行，
所有租户的下一次检索都按新参数跑。让任意租户账号能改它，等于让一个租户把别人的检索质量
换掉。理由与配置中台（#93）、模型配置（#94）逐字同源，长论证在
``app/api/routes/console_settings.py`` 的模块文档里，这里只引用不重复。

两条接口，一条读一条写：

* ``GET``：读操作，用 ``get_console_admin``。**它同时是前台的展示数据源**——
  租户面板的「这个档位能干什么」不能在前端写死，否则后台把 quality 的重排关掉之后，
  界面上仍写着「含重排」，用户会照着不存在的承诺去用。
* ``PATCH``：写操作，用 ``require_console_admin_write``（已含管理员角色与防跨站头，
  缺失 → ``20011``）。

**没有「增加一档」的接口**：档位名同时是套餐白名单、前端卡片与门禁判据的词表，插一行
不会让它变得可用（见 ``app/services/profile_preset_service.py`` 模块文档）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.console_deps import (
    get_console_admin,
    require_console_admin_write,
)
from app.core.console_auth import ConsoleIdentity
from app.core.profile_presets import ProfilePresetSpec
from app.db.async_session import get_async_db, get_async_transaction
from app.db.transaction import AsyncTransaction
from app.models.key_policy import PROFILES
from app.schemas.common import ApiResponse, ok
from app.schemas.console_profiles import (
    ProfilePresetData,
    ProfilePresetListData,
    ProfilePresetPatchData,
    ProfilePresetPatchRequest,
)
from app.services import profile_preset_service

router = APIRouter(prefix="/console", tags=["console"])


def _data(profile: str, spec: ProfilePresetSpec) -> ProfilePresetData:
    """注册表条目 → 响应行：字段逐个点名，不用 ``**spec`` 展开（照 ``model_configs`` 的写法）。"""

    return ProfilePresetData(
        profile=profile, mode=spec.mode, top_k=spec.top_k, rerank=spec.rerank, rewrite=spec.rewrite
    )


@router.get(
    "/profile-presets",
    response_model=ApiResponse[ProfilePresetListData],
    summary="四个质量档位的参数",
)
async def list_profile_presets(
    _identity: ConsoleIdentity = Depends(get_console_admin),
    db: AsyncSession = Depends(get_async_db),
) -> ApiResponse[ProfilePresetListData]:
    """四个档位的参数。只读，因此整条路径在异步栈上（票 #129）。"""

    presets = await profile_preset_service.list_presets_async(db)
    # 顺序沿用 `PROFILES`（speed / balanced / quality / custom），不在这里再排一道：
    # 前端按返回顺序摆卡片，词表只该有一处。
    items = [_data(profile, presets[profile]) for profile in PROFILES if profile in presets]
    return ok(ProfilePresetListData(items=items))


@router.patch(
    "/profile-presets/{profile}",
    response_model=ApiResponse[ProfilePresetPatchData],
    summary="改一个档位的参数",
)
async def patch_profile_preset(
    payload: ProfilePresetPatchRequest,
    profile: str = Path(description="档位名：speed / balanced / quality / custom"),
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: AsyncSession = Depends(get_async_db),
    tx: AsyncTransaction = Depends(get_async_transaction),
) -> ApiResponse[ProfilePresetPatchData]:
    result = await profile_preset_service.patch_preset(
        db,
        profile=profile,
        mode=payload.mode,
        top_k=payload.top_k,
        rerank=payload.rerank,
        rewrite=payload.rewrite,
        actor=identity,
    )
    return ok(
        ProfilePresetPatchData(
            before=result["before"],
            after=result["after"],
            changed=result["changed"],
        )
    )
