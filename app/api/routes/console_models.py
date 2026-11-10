"""多模型配置的 HTTP 接口（``/console/model-configs``，票 #94）。

这一层只做「取参数、调服务、拼响应」：字段校验、供应商注册表比对、被引用判定、审计与
事务全在 :mod:`app.services.model_config_service`。路由里出现 ``if provider not in ...``
的那一刻，下一个入口（CLI / 脚本）就得再写一遍同样的规则。

**为什么六条接口都只给管理员（admin-only）**：

``model_configs`` 是**平台级、无租户归属**的资源。一条配置里有「所有租户的下一次检索
用哪家的模型、用哪把凭证」——让任意一个租户账号能增删它，等于让它把别人的模型调用
换掉或删掉，这是跨租户越权而不是权限粒度问题。理由与配置中台（#93）逐字同源，长论证
写在 ``app/api/routes/console_settings.py`` 的模块文档里，这里只引用不再重复。

写操作一律 ``require_console_admin_write``（它已含管理员角色与防跨站头，缺头 → ``20011``）；
**显示原文也是写操作**：它会留下一条审计，所以同样要防跨站头，也同样是
``require_console_admin_write`` 而不是只读依赖。

默认不新增「重排模型」这一项：重排一期包着 ``LLMProvider``（ADR-0002），**跟随 fast**。
面板与文档都写这一句，否则用户会去找一个不存在的下拉。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.console_deps import get_console_admin, require_console_admin_write
from app.core.console_auth import ConsoleIdentity
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.console_models import (
    ModelConfigCreateRequest,
    ModelConfigData,
    ModelConfigDeleteData,
    ModelConfigListData,
    ModelConfigPatchData,
    ModelConfigPatchRequest,
    ModelConfigSecretData,
)
from app.services import model_config_service
from app.services.model_config_service import ConfigPatchResult, ConfigRowView

router = APIRouter(prefix="/console", tags=["console"])


def _row_data(row: ConfigRowView) -> ModelConfigData:
    """内部视图 → 响应行：逐字段点名，``api_key`` 从服务层拿到的就是 masked 形态。"""

    return ModelConfigData(
        id=row.spec.id,
        label=row.spec.label,
        provider=row.spec.provider,
        base_url=row.spec.base_url,
        model=row.spec.model,
        api_key=model_config_service.mask_key(row.spec.api_key),
        roles=list(row.roles),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _patch_data(result: ConfigPatchResult) -> ModelConfigPatchData:
    return ModelConfigPatchData(
        id=result.config_id,
        before=result.before,
        after=result.after,
        changed=list(result.changed),
    )


@router.get(
    "/model-configs",
    response_model=ApiResponse[ModelConfigListData],
    summary="全部模型接入（Key 永远 masked，附各角色指向）",
)
def list_model_configs(
    identity: ConsoleIdentity = Depends(get_console_admin),
    db: Session = Depends(get_db),
) -> ApiResponse[ModelConfigListData]:
    """列表。``api_key`` 在响应里永远是 ``sk-…abcd``；明文只从 ``/{id}/secret`` 出一次。"""

    rows = model_config_service.list_configs(db)
    return ok(ModelConfigListData(items=[_row_data(row) for row in rows]))


@router.post(
    "/model-configs",
    response_model=ApiResponse[ModelConfigData],
    summary="新增一条模型接入（供应商来自预设注册表，端点与模型名可自定义）",
)
def create_model_config(
    payload: ModelConfigCreateRequest,
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: Session = Depends(get_db),
) -> ApiResponse[ModelConfigData]:
    """新增：``label`` / ``provider`` / ``model`` 非空且 ``provider`` 属于注册表，
    否则 ``20015``（一条都不写）。写入同时留一条 ``model.create`` 审计。"""

    created = model_config_service.create_config(
        db,
        actor=identity,
        label=payload.label,
        provider=payload.provider,
        base_url=payload.base_url,
        model=payload.model,
        api_key=payload.api_key,
    )
    return ok(_row_data(created))


@router.patch(
    "/model-configs/{config_id}",
    response_model=ApiResponse[ModelConfigPatchData],
    summary="改一条模型接入（部分更新，改完下一次调用即生效）",
)
def patch_model_config(
    config_id: str,
    payload: ModelConfigPatchRequest,
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: Session = Depends(get_db),
) -> ApiResponse[ModelConfigPatchData]:
    """部分更新；校验规则与新增同一套（供应商注册表 / 非空 / 长度）。"""

    result = model_config_service.patch_config(
        db, actor=identity, config_id=config_id, changes=payload.provided()
    )
    return ok(_patch_data(result))


@router.delete(
    "/model-configs/{config_id}",
    response_model=ApiResponse[ModelConfigDeleteData],
    summary="删一条模型接入（被 fast/strong 指向时拒绝）",
)
def delete_model_config(
    config_id: str,
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: Session = Depends(get_db),
) -> ApiResponse[ModelConfigDeleteData]:
    """删一条配置。仍被任一角色指向 → ``20015``，先改指向（见服务层的同一条判定）。"""

    deleted = model_config_service.delete_config(db, actor=identity, config_id=config_id)
    return ok(ModelConfigDeleteData(id=deleted, deleted=True))


@router.get(
    "/model-configs/{config_id}/secret",
    response_model=ApiResponse[ModelConfigSecretData],
    summary="显示一次明文 Key（每次调用写一条审计）",
)
def reveal_model_config_secret(
    config_id: str,
    identity: ConsoleIdentity = Depends(require_console_admin_write),
    db: Session = Depends(get_db),
) -> ApiResponse[ModelConfigSecretData]:
    """返回一次明文。二次确认由前端负责；后端每次都记账（``model.reveal``）。"""

    plaintext = model_config_service.reveal_secret(db, actor=identity, config_id=config_id)
    return ok(ModelConfigSecretData(id=config_id, api_key=plaintext))
