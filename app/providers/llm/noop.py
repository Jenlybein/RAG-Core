"""空实现：骨架期不接任何大模型。调用即明确报错，不静默降级。"""

from app.providers.llm.base import LLMProvider


class NoopLLMProvider(LLMProvider):
    async def chat_json(self, *, system: str, user: str) -> dict:
        raise NotImplementedError("LLM 能力在阶段 4 才接入，骨架期不使用")