"""查询改写：把用户那句口语问法改写成文档表述（**只喂向量一路与重排**）。

它在 GLOSSARY「查询优化」里占另一半，也是父 spec 降级矩阵里「查询改写失败（含超时、
空返回）→ 降级：用原句继续」这一行的实现。

四条不许动的约定：

1. **默认关闭**（``QUERY_REWRITE_ENABLED``）：关闭时这里连大模型都不装配——离线测试、
   CI、没配 key 的机器靠这一条保持「默认路径一次模型都不调」。
2. **只做改写，不做问答**：提示词明确「不回答、不解释、不扩展、不编造」，并要求一条
   不超过 30 字的中文短句。改写错了只是召回得不好；让模型顺手答一句，检索就会拿一段
   编造的内容去查库。
3. **失败即降级**：超时 / 抛错 / 空返回 / 空串 / 结构不合法一律回退原句，并给出原因
   ``query rewrite failed``——一因一码，绝不把异常文本拼进去（调用方按码做判断）。
   检索照常继续，**绝不整单失败**。
4. **不吞取消信号**：``except Exception`` 只兜业务失败，``CancelledError`` /
   ``SystemExit`` 这类 ``BaseException`` 继续上抛（与 ``pipeline`` / ``rerank`` 的理由
   相同：被取消的请求不该把剩下的活干完）。

**用 fast 模型角色**：改写是召回之前的必经一步，延迟直接加在用户等待上。角色 ≠ 套餐
≠ 第 5 步的质量档位（GLOSSARY）；温度在 Provider 里对 fast 角色固定为 0，所以同样的
请求两次改写结果一致，「可复现」这条承诺不会在这里破功。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel

from app.core.config import settings
from app.providers.llm.base import LLMProvider

logger = structlog.get_logger(__name__)

# 降级原因：一因一码。与通道故障（``pipeline.REASON_*``）和重排失败
# （``rerank.REASON_RERANK_FAILED``）同一层级，文案写死，不随异常信息变化。
REASON_REWRITE_FAILED = "query rewrite failed"

# 系统提示词只描述「怎么改写」，绝不描述「怎么回答」：给模型一个答题的角色，
# 它就会顺手把答案写进 effective_query，而那段文字会原样拿去查库。
_SYSTEM_PROMPT = (
    "你是检索查询改写器：把用户的一句口语化问题改写成一条适合在文档库里检索的中文短句。"
    "只做改写，不回答用户的问题、不解释、不扩展话题、不编造用户没有提供的信息。"
    "改写结果不超过 30 个字，只输出改写后的那句话本身。"
)


class QueryRewrite(BaseModel):
    """结构化输出的形状：一个 ``query`` 字段。

    用 schema 而不是自由文本，是为了让「模型说了什么」有可校验的形状——各家在结构化
    输出上的差异由大模型层消化。空串由本模块判为失败（那是「模型什么都没改出来」，
    不是「用户问了个空问题」）。
    """

    query: str


@dataclass(frozen=True)
class RewriteOutcome:
    """一次改写尝试的全部事实：最终用哪句、有没有真的改写、失败原因、耗时。

    ``query`` 是**可以直接拿去召回**的那一句：成功时是改写结果，任何失败路径上都是
    原句。调用方不需要自己判断「失败了要不要回退」——那正是最容易漏掉的一步。
    """

    query: str
    applied: bool
    reason: str | None
    duration_ms: float | None


def _build_user_prompt(query: str, hint: str | None) -> str:
    """用户原话 + 可选的知识库名称 / 描述。

    ``hint`` 是**可选**的：多库检索时它是几个库的名字与描述拼起来的一句话，用来提示
    这堆文档是讲什么的；单库或调用方拿不到时就不给，提示词退化成「只有原话」。
    """

    lines = [f"用户原话：{query}"]
    if hint:
        lines.append(f"知识库：{hint}")
    lines.append("请输出改写后的检索语句。")
    return "\n".join(lines)


def _coerce(raw: Any) -> QueryRewrite:
    """把 ``LLMProvider`` 的返回值收敛成 schema 实例；不认识的形状直接抛错。

    正常路径（LangChain 实现）已经是本 schema 的实例；这里再兜一层 dict / 其他
    ``BaseModel``，是因为 ``LLMProvider`` 抽象允许这两种返回值。抛出去的错误由
    :func:`rewrite_query` 统一降级——**不**在这里吐一个空串冒充「模型没说话」。
    """

    if isinstance(raw, QueryRewrite):
        return raw
    if isinstance(raw, BaseModel):
        return QueryRewrite.model_validate(raw.model_dump())
    if isinstance(raw, dict):
        return QueryRewrite.model_validate(raw)
    raise ValueError(f"查询改写返回了不认识的形状：{type(raw).__name__}")


def _llm_provider() -> LLMProvider:
    """取 fast 模型角色的大模型。

    **惰性 import**：关闭改写时（缺省路径）不加载大模型层，离线启动不为一项没开的
    能力付出 import 代价；同时让测试的替换点仍然只有 ``app.providers.llm`` 一处。
    """

    from app.providers.llm import get_llm_provider

    return get_llm_provider("fast")


async def rewrite_query(
    *, query: str, hint: str | None = None, enabled: bool | None = None
) -> RewriteOutcome:
    """改写成文档表述；任何失败都回退原句并给出固定原因。

    ``enabled`` 是**这次调用**要不要改写，缺省读服务端开关。做成入参的理由是 #78 之后
    「要不要改写」不再只有部署一个来源：它是「档位想要 ∧ 本机装了」的合取，而合取的
    计算在平台层（``app.services.tier_policy``）已经做完了。这里再自己去读一次
    ``settings`` 就等于有第二个真相——档位说不要、机器说装了，谁说了算会变成排障时的
    第一道谜题，而且两条入口可能各读出不同的答案。
    """

    if not (settings.QUERY_REWRITE_ENABLED if enabled is None else enabled):
        # 关闭 = 「没有这一步」，不是「改了一遍恰好没变」：不装配模型、不记耗时。
        return RewriteOutcome(query, applied=False, reason=None, duration_ms=None)

    started = time.perf_counter()
    try:
        # 装配也放在 try 里：供应商名字拼错时工厂会抛 ``ValueError``，而「改写用不了」
        # 不该让每一次检索都整单失败——它是一项可选优化，不是检索的前提。
        provider = _llm_provider()
        raw = await asyncio.wait_for(
            provider.chat_structured(
                QueryRewrite,
                system=_SYSTEM_PROMPT,
                user=_build_user_prompt(query, hint),
            ),
            # 毫秒配置换成秒：``asyncio.wait_for`` 只认秒。超时是**这一层**兜住的，
            # 不能指望 HTTP 超时——那会让一个慢调用把整次检索拖到客户端先放弃。
            timeout=settings.QUERY_REWRITE_TIMEOUT_MS / 1000,
        )
        rewritten = _coerce(raw).query.strip()
    except Exception as exc:  # noqa: BLE001 - 改写失败必须降级，绝不冒泡成整单失败
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.warning(
            "query rewrite failed",
            duration_ms=duration_ms,
            # 只记异常类型，不记原文：异常文案可能夹带用户提问或提示词。
            error=type(exc).__name__,
        )
        return RewriteOutcome(
            query, applied=False, reason=REASON_REWRITE_FAILED, duration_ms=duration_ms
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    if not rewritten:
        # 空返回 / 空串同样算失败：拿空串去嵌入等于查了个空问题，而且调用方看不出
        # 「这次是模型没说话」还是「用户问了个空问题」。
        logger.warning("query rewrite returned nothing", duration_ms=duration_ms)
        return RewriteOutcome(
            query, applied=False, reason=REASON_REWRITE_FAILED, duration_ms=duration_ms
        )

    logger.info("query rewrite applied", duration_ms=duration_ms)
    return RewriteOutcome(rewritten, applied=True, reason=None, duration_ms=duration_ms)
