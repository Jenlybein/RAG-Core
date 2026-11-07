"""空实现：没配任何模型 key 时工厂返回它。

两个方法都抛 ``LLM_FAILED``，不静默降级——「这项能力这台机器上没有」必须能传到
调用方的业务码里，而不是变成一个空 dict 继续往下跑，让人以为模型答了「没有」。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.exceptions import AppError, ErrorCode
from app.providers.llm.base import LLMProvider

_UNAVAILABLE = "未配置大模型 key，LLM 能力在此环境不可用"


class NoopLLMProvider(LLMProvider):
    async def chat_structured(
        self,
        schema: type[BaseModel] | dict[str, Any],
        *,
        system: str,
        user: str,
    ) -> BaseModel | dict[str, Any]:
        raise AppError(ErrorCode.LLM_FAILED, _UNAVAILABLE)

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AppError(ErrorCode.LLM_FAILED, _UNAVAILABLE)
