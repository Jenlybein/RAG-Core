"""LLMProvider 抽象。

注意：本项目用大模型做查询改写 / 重排 / Research，不生成最终答案。

两个方法分工：``chat_structured`` 是主接口（先定字段再让模型填，各家差异由框架消化）；
``chat_json`` 是给「字段事先不确定」的调用点的薄包装。业务层只认这个 ABC——
框架的消息对象、模型实例都不出现在这里的签名上。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class LLMProvider(ABC):
    @abstractmethod
    async def chat_structured(
        self,
        schema: type[BaseModel] | dict[str, Any],
        *,
        system: str,
        user: str,
    ) -> BaseModel | dict[str, Any]:
        """按给定 schema 取结构化结果；拿不到合法结果就是失败，不退化成纯文本。"""

    @abstractmethod
    async def chat_json(self, *, system: str, user: str) -> dict:
        """取一段 JSON 对象：字段不定时的入口，语义与上一条相同。"""
