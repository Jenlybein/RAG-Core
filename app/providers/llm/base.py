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
    @property
    def available(self) -> bool:
        """这项能力**这台机器上装没装**（不是「这次答得好不好」）。

        与 ``KeywordSearchProvider.available`` / ``RerankProvider.available`` 是**同一个
        信号、同一个名字**：空实现（没配 key）返回 ``False``，于是调用点可以连一次注定
        失败、还要计一次成本的调用都省掉。三个 Provider 用同一个词是刻意的——各叫各的
        （曾经一个叫 ``enabled``、一个叫 ``available``）会让人以为它们不是一件事。

        票 #149 是第一个需要它的模型调用点：Research 的生成阶段要区分「强模型没配」
        （直接换快模型，不白问一次）与「强模型调用失败」（已经付过钱，兜底另记一笔）。
        """

        return True

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
