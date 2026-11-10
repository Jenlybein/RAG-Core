"""控制面「按 ``log_id`` 读回瀑布」接口（``/console/retrieval-traces/...``，#103）。

与 :mod:`app.api.routes.console`（登录 / 套餐 / 账号 / 审计）分成两个路由模块，各自
``include_router``：并行票共用同一个文件会让每一票的 diff 互相重叠；本模块只带
``prefix="/console"``，不重复那一条。

两条边界写在这里，因为它们都是「少一个参数」的事故：

- **走读门 ``require_console_tenant``**：会话 cookie + 租户账号。管理员（``tenant_id IS
  NULL``）一律 ``20011``——它不属于任何租户，而瀑布图恰恰是租户内的数据；不带防跨站头，
  因为看瀑布不改任何状态。
- **租户只来自会话**：路径里只有 ``log_id``，没有 ``tenant_id``，也没有「用哪把 Key」。
  归属校验在 :func:`app.services.retrieval_waterfall.waterfall_of` 里做，跨租户与不存在
  同为 ``20016``。

观测服务的读入口由 :func:`app.api.console_deps.get_langfuse_reader` 注入（测试的接缝），
密钥不进这一层，也就不可能进响应。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.api.console_deps import (
    get_langfuse_reader,
    require_console_tenant,
    tenant_id_of,
)
from app.core.console_auth import ConsoleIdentity
from app.db.session import get_db
from app.schemas.common import ApiResponse, ok
from app.schemas.console_traces import RetrievalWaterfallData, waterfall_data
from app.services.retrieval_waterfall import RemoteStageReader, waterfall_of

router = APIRouter(prefix="/console", tags=["console"])


@router.get(
    "/retrieval-traces/{log_id}/waterfall",
    response_model=ApiResponse[RetrievalWaterfallData],
    summary="按 log_id 读回一次检索的六段瀑布（先本地记录、后观测服务）",
)
def retrieval_waterfall(
    log_id: str = Path(..., min_length=1, description="归属索引的行 id，即 log_id"),
    identity: ConsoleIdentity = Depends(require_console_tenant),
    remote_reader: RemoteStageReader = Depends(get_langfuse_reader),
    db: Session = Depends(get_db),
) -> ApiResponse[RetrievalWaterfallData]:
    """六段名称与耗时；两处都没记上时返回结构化的「未启用 / 无记录」，不是 404。

    这里刻意**不**把「没记录」也做成 404：404 在这一层只有一个含义——这个 ``log_id``
    对当前租户不存在（跨租户同样如此）。而「存在、但观测没开着」是**正常状态**，
    前端要靠 ``enabled`` / ``reason`` 说出「没配」而不是画一张空卡片。
    """

    waterfall = waterfall_of(
        db,
        log_id=log_id,
        tenant_id=tenant_id_of(identity),
        remote_reader=remote_reader,
    )
    return ok(waterfall_data(waterfall))
