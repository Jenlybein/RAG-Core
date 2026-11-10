"""数据面与控制面共享的上传解析件（**HTTP 层**，不是业务逻辑）。

``POST /api/v1/documents/upload`` 与 ``POST /console/knowledge-bases/{kb_id}/documents``
是同一条规则的两次出现：单端点按 content-type 分流，multipart = 文件模式，其余 = JSON 模式。
两端各写一遍的结果是 #82 发现的逐字复制（分流、缺字段的 20002 错误体、非法 JSON 的兜底、
``request_id`` 的取值来源各约 20 行）——补一条字段、改一句文案就要改两处，而改漏一处的
症状是「一个入口 20002、另一个入口 500」。

所以这些只属于 HTTP 边界的东西收在这里。**错误码与文案逐字不变**（``20002`` + 只回
``loc`` / ``msg`` / ``type``）：既有测试（``tests/test_upload_async.py`` /
``tests/test_console_kbs.py``）是这条的守卫。

为什么不叫「上传服务」：这里不判白名单、不碰数据库、不投递任务。那些仍然分别留在两个
路由里——数据面按 API Key 的 policy 圈范围，控制面只认会话租户；把两者收进一个函数才是
真的越界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

#: 缺字段与非法 JSON 的 20002 错误体。**逐字**照抄分流实现里的那三份常量（文案是契约）：
#: ``loc`` 用元组、``type=missing`` / ``json_invalid``，与 FastAPI 原生报错同形。
_MISSING_KB_ID_ERROR: dict[str, Any] = {
    "type": "missing",
    "loc": ("body", "kb_id"),
    "msg": "Field required",
}
_MISSING_FILE_ERROR: dict[str, Any] = {
    "type": "missing",
    "loc": ("body", "file"),
    "msg": "Field required",
}
_INVALID_JSON_ERROR: dict[str, Any] = {
    "type": "json_invalid",
    "loc": ("body",),
    "msg": "JSON decode error",
}


@dataclass(frozen=True)
class UploadInput:
    """一次上传请求被解析成的统一形状：两条分支的差集在这里显式化。

    ``is_file`` 记录这次走的是哪条分支（只为人读与断言，不参与任何判定）。
    文件模式：``filename`` 非空、``data`` 是读进来的字节、``content`` 是空串（正文由 Worker
    解析落盘文件）；JSON 模式：``filename`` 为 ``None``、``data`` 是空字节、``content`` 是正文。
    这些缺省值与 ``document_service.upload`` / ``console_kb_service.upload_document`` 的
    默认参数同义，于是两条路由都能用**同一个调用**把整个形状交给服务层。
    """

    is_file: bool
    kb_id: str | None
    title: str | None
    filename: str | None
    data: bytes
    content: str
    request_id: str | None


def submitted_request_id(request: Request) -> str | None:
    """关联标识来自中间件写进 scope 的同一个值，不直接读 structlog contextvars。

    数据面与控制面的上传 / 重建在 Worker 里落到同一条流水线，日志靠同一个号牌才串得
    起来；两边各取一次「自己那套」来源，正是「一次上传在两边对不上号」的成因。
    """

    stored = request.scope.get("state", {}).get("request_id")
    return stored if isinstance(stored, str) else None


async def parse_upload_request(
    request: Request,
    *,
    kb_id_field: str | None,
    title_field: str | None,
    file: UploadFile | None,
    json_model: type[BaseModel],
    require_form_kb_id: bool = False,
) -> UploadInput:
    """按 content-type 解析一次上传，缺什么补什么、错什么按 20002 拒。

    ``require_form_kb_id`` 是两条路由唯一的形态差：数据面的 ``kb_id`` 在表单里（multipart
    必须给），控制面的 ``kb_id`` 在路径里（表单里根本没有这个字段）。JSON 分支的
    ``kb_id`` 由各自的请求模型给出（数据面必填、控制面模型里不存在）。

    ``json_model`` 必须提供 ``title`` 与 ``content`` 两个字段（两个入口的模型都有）；
    ``kb_id`` 存在时才会被带走。
    """

    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        if require_form_kb_id and (kb_id_field is None or not kb_id_field.strip()):
            raise RequestValidationError([_MISSING_KB_ID_ERROR])
        if file is None:
            raise RequestValidationError([_MISSING_FILE_ERROR])
        # 第二道关卡（白名单）在调用方、**读正文之前**做：被拒的上传不该把字节读进来。
        return UploadInput(
            is_file=True,
            kb_id=kb_id_field if require_form_kb_id else None,
            title=title_field,
            filename=file.filename or "",
            data=await file.read(),
            content="",
            request_id=submitted_request_id(request),
        )

    try:
        payload = json_model.model_validate(await request.json())
    except ValidationError as exc:
        # 交给既有的 RequestValidationError 处理器：20002 + 只挑 loc/msg/type，
        # 不回显 input / ctx / url。
        raise RequestValidationError(exc.errors()) from None
    except ValueError:
        # 半截 / 非法 JSON 也归入 20002，不要让它穿成未捕获异常的 500。
        raise RequestValidationError([_INVALID_JSON_ERROR]) from None

    return UploadInput(
        is_file=False,
        kb_id=getattr(payload, "kb_id", None),
        title=getattr(payload, "title", None),
        filename=None,
        data=b"",
        content=getattr(payload, "content", ""),
        request_id=submitted_request_id(request),
    )
