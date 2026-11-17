"""「这条问题有没有检索意图」——由模型判，判定跟着那一次结构化调用走。

## 为什么不另发一次判定调用

三条模型端点**本来就要调一次大模型**：``/rewrite`` 要改写、``/decompose`` 与
``/research`` 要规划。把意图识别做成那次调用的一个 schema 字段，成本增量是几个输出
token，而不是又一次往返（以及又一次超时风险）。真正的节省发生在后面：模型说
``no_content`` 时，端点在任何 **嵌入 / 检索 / 重排 / 记账** 之前就停——``/decompose``
省掉 N 次内核，``/research`` 省掉子检索、补证与生成。

## 为什么是模型判而不是字符规则

字符规则只看「长什么样」，测了两次就被证否：``啊啊啊啊…a`` 全部漏判，补上占比规则
又把 ``E1025`` 误杀。判「这句话想干什么」是语义问题，只有模型能答。

## 三条约定的顺序

1. **缺省即放行**：``intent`` 的默认值是 ``search``。模型没给、格式坏了、老调用点没
   传，一律按「有内容」继续跑——判定失败不该变成「所有请求都被拒」。
2. **拒就是拒**：``no_content`` 抛 ``20033``，**不降级**成「原句直查」。降级会让调用方
   以为改写 / 分解真的发生了（与 ``20013`` 那条「名字是契约」同一条纪律）。
3. **只判一次**：判定跟着端点自己的第一次模型调用走，不重复判、不二次确认。
"""

from __future__ import annotations

from typing import Literal

import structlog

from app.core.exceptions import AppError, ErrorCode

logger = structlog.get_logger(__name__)

#: schema 里 ``intent`` 的类型。用 ``Literal`` 而不是自由字符串：``chat_structured``
#: 会把枚举塞进 JSON Schema，模型拿到的是一份穷举清单，而不是一段口头约定。
QueryIntent = Literal["search", "no_content"]

#: 「有检索意图」——默认值，也是所有异常路径的归宿。
INTENT_SEARCH: QueryIntent = "search"

#: 「没有可检索的内容」：只有标点 / 符号 / 感叹词 / 纯噪声，没有想问的东西。
INTENT_NO_CONTENT: QueryIntent = "no_content"

#: 两个取值（顺序即文档里的顺序）。
QUERY_INTENTS: tuple[QueryIntent, ...] = (INTENT_SEARCH, INTENT_NO_CONTENT)


def ensure_search_intent(*, intent: str | None, reason: str | None = None) -> None:
    """模型判了 ``no_content`` 就抛 ``20033``；其余（含缺省）一律放行。

    ``reason`` 只进日志与错误提示，不进响应契约：它是「模型为什么这么判」的一句话，
    用于线上排查，不是给调用方解析的字段。
    """

    if intent != INTENT_NO_CONTENT:
        return
    logger.info(
        "query judged to have no search intent",
        # 只记长度与原因摘要，不记原句：用户提问不进日志。
        reason=(reason or "")[:80],
    )
    raise AppError(
        ErrorCode.QUERY_HAS_NO_CONTENT,
        "query has no searchable content: the model judged that it asks for nothing to retrieve",
    )


__all__ = [
    "INTENT_NO_CONTENT",
    "INTENT_SEARCH",
    "QUERY_INTENTS",
    "ensure_search_intent",
]
