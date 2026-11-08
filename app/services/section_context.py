"""节说明：给每个节生成一句「本节在讲什么」，拼进检索文本。

为什么按节、不按片：一个知识库里节的数量比片少一个数量级（本仓库 456 片对 361 节，
多片节只有 14 个），按片调用会把成本与索引时长放大十倍以上；而「这片属于什么场景」
本来就是节的属性，不需要逐片重算。

为什么放在检索文本而不是 `content`：`content` 是切片原文，要原样返回给调用方；
检索文本只在索引期算出来、只进向量与关键词索引。两者的分工见 GLOSSARY。

失败即降级：拿不到说明时回落成现状（只有文档名 + 章节路径），**绝不阻断索引**——
这是一项可选增强，不是索引的前提。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from pydantic import BaseModel

from app.core.config import settings

logger = structlog.get_logger(__name__)


class SectionContext(BaseModel):
    """模型的结构化输出：一句话说明本节在讲什么。"""

    context: str


_SYSTEM_PROMPT = (
    "你在为知识库的检索索引做标注。给定一个文档章节的标题与正文，"
    "用一句不超过 40 字的中文说明「这一节在讲什么、读者会在什么场景下需要它」。"
    "要求：只陈述事实，不要评价，不要罗列正文里的数字，不要重复标题。"
)

# 一次生成的最少有效字数：更短的输出（「本节讲部署」这类）没有区分度，
# 留在检索文本里只会稀释真正的关键词。
_MIN_CONTEXT_CHARS = 8


def _llm_provider() -> Any:
    """惰性 import：关闭节说明时不为一项没开的能力付 import 代价。"""

    from app.providers.llm import get_llm_provider

    return get_llm_provider("fast")


def _build_user_prompt(*, title: str, heading_path: str, section_text: str) -> str:
    body = section_text.strip()[: settings.SECTION_CONTEXT_MAX_INPUT_CHARS]
    return f"文档：{title}\n章节：{heading_path}\n正文：\n{body}"


def _coerce(raw: Any) -> str:
    """把模型返回的任意形态收敛成一句说明；拿不到就返回空串。"""

    if isinstance(raw, SectionContext):
        return raw.context.strip()
    if isinstance(raw, dict):
        return str(raw.get("context") or "").strip()
    return ""


async def summarize_sections(
    sections: list[tuple[str, str, str]], *, title: str
) -> dict[str, str]:
    """为给定节生成说明，返回 ``{section_id: 说明}``。

    ``sections`` 是 ``[(section_id, heading_path, 本节全文)]``，已经按 section_id
    去重——调用方负责把同节的多片折叠成一条，这里只管「一节一次」。
    """

    if not sections or not settings.SECTION_CONTEXT_ENABLED:
        return {}

    summaries: dict[str, str] = {}
    provider: Any | None = None
    for section_id, heading_path, section_text in sections:
        started = time.perf_counter()
        try:
            if provider is None:
                # 装配放在循环里第一次：供应商名字拼错时工厂会抛，不该让索引整体失败。
                provider = _llm_provider()
            raw = await asyncio.wait_for(
                provider.chat_structured(
                    SectionContext,
                    system=_SYSTEM_PROMPT,
                    user=_build_user_prompt(
                        title=title,
                        heading_path=heading_path,
                        section_text=section_text,
                    ),
                ),
                timeout=settings.SECTION_CONTEXT_TIMEOUT_MS / 1000,
            )
            context = _coerce(raw)
            if len(context) >= _MIN_CONTEXT_CHARS:
                summaries[section_id] = context
        except Exception as exc:  # noqa: BLE001 - 拿不到说明就降级，不阻断索引
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.warning(
                "section context failed",
                duration_ms=duration_ms,
                # 只记异常类型：文案可能夹带正文。
                error=type(exc).__name__,
            )
            # 装配失败（拿不到 provider）时后续节也必然失败，直接放弃这次尝试；
            # 单节超时或单节返回异常则只跳过这一节——「失败即降级」意味着坏掉的
            # 一节不该连累其余节失去说明。
            if provider is None:
                break
    return summaries
