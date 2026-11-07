"""查询优化的编排入口：**先改写、再同义词扩展**（召回之前的两步）。

它是 GLOSSARY「检索链路」的第一格：查询优化 → 召回 → 融合 → 重排。把它单独放一个
函数而不是散在 ``rag_service`` 里，是因为「两路拿不同查询」这件事需要一个显式的
载体（:class:`QueryProcessResult`）——散在流程里时，后人很容易顺手把两路都改成同一个
变量，而这**不会报错**，只会静默地把一半功能关掉。

**顺序不许反**：改写补语义、同义词补字面。先扩展后改写，同义词表就只能匹配用户原话；
而改写恰恰会把口语换成文档里那种说法（「退钱」→「退款」），词表里配的往往是后者。

**两个开关各自独立、都默认关闭**：关掉时这一步是恒等变换——``raw`` / ``effective`` /
``search`` 三句相等，检索行为与票 4 逐字段一致（回归红线）。

谁用哪一句（这一句是本模块存在的理由）：

* ``effective_query`` → 向量一路**和重排**（语义那一侧，含"给模型看的原始问题"）；
* ``search_query`` → 关键词一路（字面那一侧，同义词补的就是它）。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass

from app.core.config import settings
from app.services.query.rewrite import rewrite_query
from app.services.query.synonyms import SynonymGroup, expand_synonyms


@dataclass(frozen=True)
class QueryProcessResult:
    """一次查询优化的结果：**两路各自的查询** + 降级事实 + 耗时。

    ``effective_query`` 与 ``search_query`` 不同是**设计**，不是 bug：同义词补的是字面
    匹配，对语义召回没有意义；改写补的是语义，对字面匹配没好处。硬把它们合成一句，
    等于用其中一路的优化去污染另一路。

    ``rewrite_degraded`` / ``rewrite_reason`` 是降级的**唯一来源**：一因一码，原因写死成
    ``query rewrite failed``，不拼异常文本。上游把它原样并进 ``metadata.retrieval``，
    调用方不必自己判断「这次结果是不是打了折」。
    """

    raw_query: str
    effective_query: str
    search_query: str
    rewrite_applied: bool
    rewrite_degraded: bool
    rewrite_reason: str | None
    duration_ms: float


async def optimize_query(
    *,
    query: str,
    synonym_groups: Iterable[SynonymGroup] = (),
    rewrite_hint: str | None = None,
) -> QueryProcessResult:
    """改写 → 同义词扩展，返回两路各自该用的查询。

    ``synonym_groups`` 与 ``rewrite_hint`` 都由调用方传进来：本模块不查库、不认识
    ``KnowledgeBase``。多库检索时调用方在可见性预校验那一次已经把 ``settings`` 拿在手上，
    这里再查一次库就会变成「每个库两次查询」。
    """

    started = time.perf_counter()

    rewrite = await rewrite_query(query=query, hint=rewrite_hint)
    # 失败路径上 ``rewrite.query`` 已经是原句——回退这件事在改写阶段就做完了，
    # 这里不需要（也不该）再判断一次。
    effective_query = rewrite.query

    if settings.SYNONYM_EXPANSION_ENABLED:
        # 匹配的是 ``effective_query``：改写之后的句子才是真正拿去检索的那一句。
        search_query = expand_synonyms(effective_query, synonym_groups)
    else:
        # 关闭 = 没有这一步：关键词一路与向量一路拿同一句（与票 4 完全一致）。
        search_query = effective_query

    return QueryProcessResult(
        raw_query=query,
        effective_query=effective_query,
        search_query=search_query,
        rewrite_applied=rewrite.applied,
        rewrite_degraded=rewrite.reason is not None,
        rewrite_reason=rewrite.reason,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
