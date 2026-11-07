"""LangChain 版大模型 Provider：唯一把框架类型关在里面的一层。

**为什么错误要在这里统一成 ``LLM_FAILED``**：各家的失败长相完全不同——OpenAI 系抛
``openai.APIError``、Anthropic 抛自己的异常类、结构化输出解析失败抛 pydantic 校验错，
超时又是另一套。业务层不该认识任何一家，也不该为「换一家」改一次 except 子句；
它只认「大模型调用失败」这一个业务码。

**一个反直觉的坑**：模型一个 tool_call 都不返回时，框架的 ``with_structured_output``
不抛异常，而是**返回 None**。若不在这里拦住，「没拿到结构化结果」会被上游当成合法
结果继续跑下去，最后表现成「改写无效」「重排乱序」这种看不出根因的症状。

**遥测**：框架的追踪客户端只读环境变量，不读我们的 Settings，所以构造模型前要把开关
同步过去（``apply_framework_telemetry_switches``）；否则 .env 里写 false 只是文档。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.utils.json import parse_json_markdown
from pydantic import BaseModel

from app.core.exceptions import AppError, ErrorCode
from app.providers.langchain_runtime import apply_framework_telemetry_switches
from app.providers.llm.base import LLMProvider

__all__ = ["LangChainLLMProvider", "apply_framework_telemetry_switches"]


def _messages(system: str, user: str) -> list[Any]:
    return [SystemMessage(content=system), HumanMessage(content=user)]


def _text_of(message: BaseMessage) -> str:
    """把消息正文取成纯文本。

    Anthropic 的 content 可能是内容块列表而不是字符串；取文本块拼接，
    碰到纯图片/工具块的应答就等同于空字符串——交给下游的 JSON 解析去报错。
    """

    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


class LangChainLLMProvider(LLMProvider):
    def __init__(self, *, chat_model: BaseChatModel, model_name: str) -> None:
        self._chat_model = chat_model
        self.model_name = model_name

    @property
    def chat_model(self) -> BaseChatModel:
        """构造时的底层模型（只读）。

        给排障与测试用：想知道「这台机器到底在打哪个地址、哪一档」时不必猜环境变量。
        业务层不要拿它直接发请求——那等于把框架漏进业务编排（ADR-0011）。
        """

        return self._chat_model

    async def chat_structured(
        self,
        schema: type[BaseModel] | dict[str, Any],
        *,
        system: str,
        user: str,
    ) -> BaseModel | dict[str, Any]:
        try:
            runner = self._chat_model.with_structured_output(schema)
            result = await runner.ainvoke(_messages(system, user))
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 - 各家异常都要收敛成一个业务码
            raise AppError(ErrorCode.LLM_FAILED, f"结构化输出调用失败：{exc}") from exc

        if result is None:
            raise AppError(ErrorCode.LLM_FAILED, "模型没有返回结构化结果")
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            if not isinstance(result, schema):
                raise AppError(ErrorCode.LLM_FAILED, "模型返回的结构不符合约定 schema")
        elif not isinstance(result, dict):
            raise AppError(ErrorCode.LLM_FAILED, "模型返回的结构不是 JSON 对象")
        return result

    async def chat_json(self, *, system: str, user: str) -> dict:
        """普通对话 + 框架的 JSON 解析器。

        ``chat_json`` 存在的意义就是「字段事先不确定」，而 ``with_structured_output``
        必须先有一份 schema（Tool 定义），所以它不走那条路；解析仍交给框架
        （``parse_json_markdown`` 会剥 ```json 围栏），失败映射与结构化输出一致。
        """

        try:
            message = await self._chat_model.ainvoke(_messages(system, user))
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrorCode.LLM_FAILED, f"JSON 调用失败：{exc}") from exc

        try:
            payload = parse_json_markdown(_text_of(message))
        except Exception as exc:  # noqa: BLE001 - json.JSONDecodeError 及框架的包装异常
            raise AppError(ErrorCode.LLM_FAILED, "模型返回的不是合法 JSON") from exc

        if not isinstance(payload, dict):
            raise AppError(ErrorCode.LLM_FAILED, "模型返回的 JSON 不是对象")
        return payload
