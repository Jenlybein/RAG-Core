"""数据面身份查询：``GET /api/v1/auth/me``（票 #72 收尾）。

给**外部程序**查「我是谁、买的是哪一档、今天用了多少」——与控制面 ``/console/me`` 是
两扇不同的门，刻意不合并：这一扇只认 ``Authorization: Bearer`` 里的 API Key，控制面
只认会话 cookie（spec #72「两个 /me 分工」）。两边返回的套餐与用量形状完全一致
（见 :mod:`app.schemas.auth`），鉴权方式不同。

用量**没有第二种读法**：调的是 :func:`app.services.quota_gate.read_tenant_daily_usage`，
控制面读的是同一个函数、同一个租户级日桶（UTC、按天分桶）。开关关着时它返回 ``None``，
经共享的 :func:`app.schemas.console.usage_data` 映射成 ``source="not_tracked"``，
绝不补成 0。

本路由不判任何规则：身份由 ``get_current_key``（唯一判定内核的 HTTP 适配器）给出，
套餐就在那份结论里。它不检索，因此不经白名单、限流与日配额——读余量本身不消耗额度。
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_key
from app.core.auth import DevKeyContext
from app.schemas.auth import AuthMeData, AuthTenantData
from app.schemas.common import ApiResponse, ok
from app.schemas.console import plan_snapshot_data, usage_data
from app.services.quota_gate import read_tenant_daily_usage

router = APIRouter()


@router.get(
    "/auth/me",
    response_model=ApiResponse[AuthMeData],
    summary="查自己的租户、套餐与今日用量（外部程序）",
)
def me(key: DevKeyContext = Depends(get_current_key)) -> ApiResponse[AuthMeData]:
    """按这把 Key 的判定结论答三栏；不读任何跨租户的行，也不写任何计数。

    ``tenant`` / ``plan`` 原样来自上下文；``usage`` 的 ``limit`` 取套餐的日配额，
    ``used`` 走只读的租户日桶——与控制面 ``/me`` 是同一份口径（开关关着时如实
    报「未统计」，不是 0）。
    """

    return ok(
        AuthMeData(
            tenant=AuthTenantData(tenant_id=key.tenant_id, name=key.tenant_name),
            plan=plan_snapshot_data(key.plan),
            usage=usage_data(
                used=read_tenant_daily_usage(tenant_id=key.tenant_id),
                limit=key.plan.retrieve_daily,
            ),
        )
    )
