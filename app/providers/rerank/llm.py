"""一期的大模型重排：包着 ``LLMProvider``，自己不建 HTTP 客户端。

ADR-0011 的边界图里，``RerankProvider`` 是「我们自写」的那一类：它不直接和某一家的
重排端点说话，而是把「让模型给候选打分」表达成**一次结构化输出调用**，把各家的差异
留给大模型层去消化。二期换成独立重排端点时，换的是这个类，不是调用它的编排
（GLOSSARY「Provider（六类）」：Rerank 一期就是包着 ``LLMProvider`` 的
``LLMRerankProvider``）。

三条与「重排追求准、但不能把系统搞挂」直接相关的约定：

1. **只改 ``score``**：融合分与五列分列字段原样带回，调用方仍能手算「这条为什么进来」。
   重排一旦改写这些字段，可解释性就没了，而可解释性正是本项目的验收点。
2. **模型的一票不是无限的**：候选先封顶再送（``max_candidates``），单条内容先截断
   （``chunk_max_chars``）。提示词长度直接等于延迟与成本。
3. **失败就是失败**：空返回、结构不合法、调用失败、超时全部抛出去，**不**在这里
   退回「按原顺序返回」——那会让上层以为重排成功了，于是降级标记与原因都丢掉了。
   降级是编排层（``app/services/retrieval/rerank.py``）的职责，不是 Provider 的。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from app.providers.llm.base import LLMProvider
from app.providers.rerank.base import RerankProvider

# 结构化输出的形状：每条候选一个 0~1 的相关性分。用 schema 而不是自由文本，
# 是为了让「模型说了什么」有可校验的形状——各家在结构化输出上的差异由大模型层消化。
# 量纲取 0~1 而不是 0~10：这个分会直接落进响应的 ``score`` / ``rerank_score``，
# 而 ``vector_score``（余弦）本来就在 0~1，两个字段同一量纲才不会误导调用方。
_SYSTEM_PROMPT = (
    "你是检索结果重排器。给定一个用户问题和若干候选切片，"
    "为每条切片打一个 0 到 1 的相关性分（1 表示这条最能回答该问题）。"
    "只输出结构化结果，不要解释。chunk_id 必须原样照抄候选里的值，不要自己编。"
)


class RerankScore(BaseModel):
    """模型对一条候选的打分。"""

    chunk_id: str
    score: float


class RerankRanking(BaseModel):
    """模型必须回的结构：一个 ``ranking`` 列表。

    刻意不做「至少一条」的校验：空列表是「模型什么都没排」，属于失败，但判定放在
    Provider 里（要能给出人话错误），schema 只负责形状。
    """

    ranking: list[RerankScore]


def _build_user_prompt(query: str, candidates: list[dict], chunk_max_chars: int) -> str:
    """把候选拼成提示词：编号 + chunk_id + 标题 + 截断后的内容。

    截断只影响**送进模型的内容**，不影响回给调用方的切片原文。
    """

    lines = [f"用户问题：{query}", "", f"候选切片（共 {len(candidates)} 条）："]
    for index, chunk in enumerate(candidates, start=1):
        content = str(chunk.get("content", ""))[:chunk_max_chars]
        lines.append(
            f"{index}. chunk_id={chunk['chunk_id']} | "
            f"title={chunk.get('title', '')} | 内容：{content}"
        )
    return "\n".join(lines)


def _as_ranking(raw: Any) -> RerankRanking:
    """把 ``LLMProvider`` 的返回值收敛成 schema 实例。

    正常路径（LangChain 实现）已经是本 schema 的实例；这里再兜一层 dict / 其他
    BaseModel，是为了 Provider 抽象允许这两种返回值。结构不合法就抛 pydantic 校验错
    （``ValidationError`` 是 ``ValueError`` 的子类），由上层统一降级。
    """

    if isinstance(raw, RerankRanking):
        return raw
    if isinstance(raw, BaseModel):
        return RerankRanking.model_validate(raw.model_dump())
    return RerankRanking.model_validate(raw)


class LLMRerankProvider(RerankProvider):
    """用大模型给融合后的候选重新打分排序。

    ``llm`` 由构造参数注入（组合根装配），本类不读配置、不建客户端：它只认识
    ``LLMProvider`` 这一个抽象。``top_n`` / ``max_candidates`` / ``chunk_max_chars`` /
    ``timeout_seconds`` 来自服务端配置，由工厂读出来传进来。
    """

    def __init__(
        self,
        *,
        llm: LLMProvider,
        max_candidates: int = 20,
        chunk_max_chars: int = 1024,
        timeout_seconds: float = 15.0,
    ) -> None:
        if max_candidates < 1:
            raise ValueError(f"候选上限必须为正数，当前是 {max_candidates!r}")
        if chunk_max_chars < 1:
            raise ValueError(f"单条内容上限必须为正数，当前是 {chunk_max_chars!r}")
        if timeout_seconds <= 0:
            raise ValueError(f"重排超时必须为正数，当前是 {timeout_seconds!r}")

        self._llm = llm
        self._max_candidates = max_candidates
        self._chunk_max_chars = chunk_max_chars
        self._timeout_seconds = timeout_seconds

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        """按模型打的分重排；顺序相同分数的按融合顺序（确定性），不随机。"""

        candidates = list(chunks[: self._max_candidates])
        if not candidates:
            # 没有候选就没有可排的东西：不必花一次模型调用，也不是失败。
            return []

        # 返回条数只由调用方的 ``top_n``（= 请求的 ``top_k``）决定：服务端配置不再
        # 悄悄压条数。候选上限 ``max_candidates`` 管的是「送几条给模型」，不是「回几条」。
        limit = top_n

        raw = await asyncio.wait_for(
            self._llm.chat_structured(
                RerankRanking,
                system=_SYSTEM_PROMPT,
                user=_build_user_prompt(query, candidates, self._chunk_max_chars),
            ),
            timeout=self._timeout_seconds,
        )
        ranking = _as_ranking(raw)
        if not ranking.ranking:
            raise ValueError("重排模型没有返回任何排序")

        # 同一 chunk_id 说了两遍时取最高分：顺序必须可复现，不能取决于模型的行文顺序。
        scores: dict[str, float] = {}
        for entry in ranking.ranking:
            previous = scores.get(entry.chunk_id)
            scores[entry.chunk_id] = entry.score if previous is None else max(previous, entry.score)

        # 未知 chunk_id 天然被忽略（只按候选自己的 id 取分）；模型漏掉的候选按 0 分，
        # 于是它们退到后面但**不会消失**——重排不是过滤器。
        ordered = sorted(
            enumerate(candidates),
            key=lambda pair: (-scores.get(pair[1]["chunk_id"], 0.0), pair[0]),
        )
        return [
            # ``rerank_score`` 与 ``score`` 同值：前者是「这一路的分数」（与
            # ``vector_score`` / ``bm25_score`` / ``rrf_score`` 并排，可解释），
            # 后者是「本次最终排序分」（契约字段，重排后由它决定顺序）。
            {**chunk, "score": scores.get(chunk["chunk_id"], 0.0),
             "rerank_score": scores.get(chunk["chunk_id"], 0.0)}
            for _, chunk in ordered[:limit]
        ]
