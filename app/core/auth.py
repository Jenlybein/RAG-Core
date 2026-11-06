"""占位鉴权（第 1 步）：两个数据面入口共用一把固定 Key。

头部必须带 ``Authorization: Bearer dev-key``；通过后返回默认租户
``tenant_demo``，由各接口传给业务层做强制过滤。第 5 步会换成
``api_keys`` 表的真实校验与 ``allowed_kb_ids`` 白名单，这里先不建表。

不对或缺失时抛 ``AppError(20010)``，走第 2 步的 HTTP 入口适配器，
统一变成 HTTP 200 + 非零 ``code``。
"""

from dataclasses import dataclass

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode


@dataclass(frozen=True)
class DevKeyContext:
    """占位 Key 解出的身份；字段形状与第 5 步的 KeyContext 对齐。"""

    tenant_id: str


def resolve_dev_key(request: Request) -> DevKeyContext:
    """从请求头取占位 Key，不通过就抛业务失败（不是 401）。"""

    header = request.headers.get("authorization", "")
    expected = f"Bearer {settings.DEV_API_KEY}"
    if header != expected:
        raise AppError(ErrorCode.UNAUTHORIZED)
    return DevKeyContext(tenant_id=settings.DEV_TENANT_ID)
