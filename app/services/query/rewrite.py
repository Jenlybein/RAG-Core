"""查询改写：把用户那句口语问法改写成文档表述。

第 13 步起 `/rag/rewrite` 是一条**独立端点**：调用方选它就是要花这一次大模型调用，
所以「同义词 + 改写」这条加工链只在这里发生，`/rag/retrieve` 恒等原句直查。

四条约定：

1. **只做改写，不做问答**：提示词明确「不回答、不解释、不扩展、不编造」，并要求一条
   不超过 30 字的中文短句。改写错了只是召回得不好；让模型顺手答一句，检索就会拿一段
   编造的内容去查库。
2. **失败即整单失败**：超时 / 抛错 / 空返回 / 空串 / 结构不合法一律抛
   ``AppError(ErrorCode.MODEL_CALL_FAILED)``（50003，客户重试），绝不静默回退成
   「原句直查」的伪装结果。旧串 ``query rewrite failed`` 就此退役。
3. **不吞取消信号**：``except Exception`` 只兜业务失败，``CancelledError`` /
   ``SystemExit`` 这类 ``BaseException`` 继续上抛。
4. **单一 ``default`` 模型角色**：与分解、Research 四阶段共用同一套接入，
   温度在 Provider 里固定为 0，所以同样的请求两次改写结果一致。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, field_validator

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.services.query_intent import INTENT_NO_CONTENT, INTENT_SEARCH, QueryIntent, ensure_search_intent

logger = structlog.get_logger(__name__)

# 系统提示词只描述「怎么改写」与「什么情况不改写」，绝不描述「怎么回答」：给模型一个
# 答题的角色，它就会顺手把答案写进 effective_query，而那段文字会原样拿去查库。
_SYSTEM_PROMPT = (
    "你是检索查询改写器：把用户的一句口语化问题改写成一条适合在文档库里检索的中文短句。"
    "只做改写，不回答用户的问题、不解释、不扩展话题、不编造用户没有提供的信息。"
    "改写结果不超过 30 个字，只输出改写后的那句话本身。"
    "**先判意图**：如果这句话里没有任何想查的东西（只有标点、符号、感叹词、复读的字符，"
    "或者纯粹是情绪发泄而没有信息需求），把 intent 设为 no_content、query 留空；"
    "只要它问到了任何具体的事情，intent 就是 search，然后照上面的要求改写。"
)


class QueryRewrite(BaseModel):
    """结构化输出的形状：改写结果 + 意图判定。

    用 schema 而不是自由文本，是为了让「模型说了什么」有可校验的形状——各家在结构化
    输出上的差异由大模型层消化。空串由本模块判为失败（那是「模型什么都没改出来」，
    不是「用户问了个空问题」）。

     ``intent`` **缺省 search**：模型没给这个字段、或给了不认识的值，一律按「有内容」
    继续跑。判定失败不该变成「所有请求都被拒」——这道闸是优化，不是安全边界。
     ``reason`` 只进日志与错误提示，不进响应契约。
    """

    query: str
    intent: QueryIntent = INTENT_SEARCH
    reason: str | None = None

    @field_validator("intent", mode="before")
    def _unknown_intent_means_search(cls, value: Any) -> QueryIntent:
        """不认识的取值一律当成 ``search``。

        ``Literal`` 直接校验会把「模型回了 ``no-intent`` 这种词」变成一个结构错误，
        于是整单变成 ``50003``（客户重试也修不好）。判定字段的正确失败方式是**放行**：
        它只是优化，不是安全边界。
        """

        return INTENT_NO_CONTENT if value == INTENT_NO_CONTENT else INTENT_SEARCH  # type: ignore[return-value]


@dataclass(frozen=True)
class RewriteOutcome:
    """一次改写的**成功**结果：最终用哪句、耗时。

    失败不产出一个 outcome——它抛 ``50003``。所以这里没有 ``reason`` / ``applied``
    这类「其实降级过」的字段：调用方拿到 outcome 就意味着改写真的发生了。
    """

    query: str
    duration_ms: float


def _build_user_prompt(query: str, hint: str | None) -> str:
    """用户原话 + 可选的知识库名称 / 描述。"""

    lines = [f"用户原话：{query}"]
    if hint:
        lines.append(f"知识库：{hint}")
    lines.append("请输出改写后的检索语句。")
    return "\n".join(lines)


def _coerce(raw: Any) -> QueryRewrite:
    """把 ``LLMProvider`` 的返回值收敛成 schema 实例；不认识的形状直接抛错。"""

    if isinstance(raw, QueryRewrite):
        return raw
    if isinstance(raw, BaseModel):
        return QueryRewrite.model_validate(raw.model_dump())
    if isinstance(raw, dict):
        return QueryRewrite.model_validate(raw)
    raise ValueError(f"查询改写返回了不认识的形状：{type(raw).__name__}")


async def rewrite_query(
    *, query: str, hint: str | None = None, timeout_ms: int | None = None
) -> RewriteOutcome:
    """改写成文档表述；任何失败都抛 ``50003``（客户重试），绝不静默降级。

    ``timeout_ms`` 缺省读服务端配置。端点决定「要不要改写」，所以这里不再有
    ``enabled`` 开关——调用方不调它就等于不改写。
    """

    from app.providers.llm import get_llm_provider

    started = time.perf_counter()
    try:
        provider = await get_llm_provider()
        raw = await asyncio.wait_for(
            provider.chat_structured(
                QueryRewrite,
                system=_SYSTEM_PROMPT,
                user=_build_user_prompt(query, hint),
            ),
            timeout=(timeout_ms if timeout_ms is not None else settings.QUERY_REWRITE_TIMEOUT_MS)
            / 1000,
        )
        result = _coerce(raw)
    except Exception as exc:  # noqa: BLE001 - 统一翻成可重试的业务失败
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.warning(
            "query rewrite model call failed",
            duration_ms=duration_ms,
            # 只记异常类型，不记原文：异常文案可能夹带用户提问或提示词。
            error=type(exc).__name__,
        )
        raise AppError(ErrorCode.MODEL_CALL_FAILED) from exc

    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    # 意图判定跟着**这一次**调用走：模型说「没有想查的东西」就停在这里，不降级成原句直查
    # （降级会让调用方以为改写真的发生了）。这一句必须在 try 之外：``20033`` 被包在
    # 那个 ``except Exception`` 里会被翻成 ``50003``，调用方就分不清「问题是垃圾」与
    # 「模型挂了」。
    ensure_search_intent(intent=result.intent, reason=result.reason)

    rewritten = result.query.strip()
    if not rewritten:
        logger.warning("query rewrite returned nothing", duration_ms=duration_ms)
        raise AppError(ErrorCode.MODEL_CALL_FAILED)

    logger.info("query rewrite applied", duration_ms=duration_ms)
    return RewriteOutcome(rewritten, duration_ms=duration_ms)


__all__ = ["QueryRewrite", "RewriteOutcome", "rewrite_query"]
