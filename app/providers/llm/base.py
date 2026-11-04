"""LLMProvider 抽象（阶段 4 才实现真版）。

注意：本项目用大模型做查询改写 / 重排 / Research，不生成最终答案。
"""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    @abstractmethod
    async def chat_json(self, *, system: str, user: str) -> dict: ...