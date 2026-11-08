"""与协议无关的错误词汇：分段错误码 + 业务异常载体。

权威全表来自 docs/设计决策.md「错误码全表」：全表一次性落地，3xxxx 仅预留。
分段按责任方划分：0 成功；2xxxx 是调用方能处理的业务失败；3xxxx 是存储段，
当前全表里没有成员，只以注释与本文档说明预留，不编造没有调用点的码；
4xxxx 是外部模型 / 服务请求失败；5xxxx 是服务异常与系统级失败。
后续章节只追加编号，不改既有值。

本模块不放任何 HTTP 或 JSON-RPC 知识。HTTP 入口的翻译在 app/api/error_handlers.py；
MCP 入口（第 1 步接通、第 5 步接上真实鉴权）会按自己的协议翻译同一个词汇——共享的是错误词汇，不是响应壳。
"""

from enum import IntEnum
from typing import Any

__all__ = ["AppError", "ErrorCode", "default_message"]


class ErrorCode(IntEnum):
    """错误码全表：全表一次性落地，3xxxx 仅预留。"""

    SUCCESS = 0

    # 2xxxx：业务失败（可预期、调用方能处理）
    REQUEST_VALIDATION_ERROR = 20002
    API_RATE_LIMIT = 20005
    KNOWLEDGE_BASE_NOT_FOUND = 20008
    UNAUTHORIZED = 20010
    FORBIDDEN = 20011
    FEATURE_NOT_ALLOWED = 20013
    QUOTA_EXCEEDED = 20014
    PARAM_ERROR = 20015
    NOT_FOUND = 20016
    PLAN_NOT_FOUND = 20017
    PLAN_INVALID = 20018
    PLAN_IN_USE = 20019
    FEEDBACK_FAILED = 20020
    FEEDBACK_LOG_MISMATCH = 20021
    FEEDBACK_INVALID_SCORE = 20022
    DOCUMENT_PARSE_FAILED = 20023
    DOCUMENT_CONTENT_EMPTY = 20024
    DOCUMENT_IS_PROCESSING = 20025
    FILE_TOO_LARGE = 20026
    REINDEX_REQUIRES_FORCE = 20027
    UNSUPPORTED_DOCUMENT_FORMAT = 20028

    # 3xxxx：存储段，仅预留，不定义成员（全表里没有调用点）。

    # 4xxxx：外部模型 / 服务请求失败
    API_TIMEOUT = 40001
    API_REQUEST_ERROR = 40002
    EMBEDDING_FAILED = 40003
    RERANK_FAILED = 40004
    LLM_FAILED = 40005

    # 5xxxx：服务异常与系统级失败
    SERVER_ERROR = 50000
    SYSTEM_BUSY = 50001
    RETRIEVAL_FAILED = 50002


def default_message(code: ErrorCode) -> str:
    """缺省提示：枚举名小写、下划线转空格，供 AppError 与响应构造器共用。"""

    return code.name.lower().replace("_", " ")


class AppError(Exception):
    """业务失败（可预期、调用方能处理）的载体，属于领域概念，不是 HTTP 异常。

    业务层只写 ``raise AppError(ErrorCode.X)``；「翻译成响应」由各入口适配器负责。
    """

    def __init__(
        self,
        code: ErrorCode,
        msg: str | None = None,
        *,
        data: Any = None,
    ) -> None:
        self.code = code
        self.msg = msg if msg is not None else default_message(code)
        self.data = data
        super().__init__(self.msg)
