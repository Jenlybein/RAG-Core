"""查询加工的编排入口：把用户那句话加工成「向量逐句 + 关键词一句 + 重排一句」。

第 13 步起有两种调用形态，由**端点**决定，不再由密钥配置里的开关决定：

* ``optimize_for_direct``：恒等变换，三句全等于原句（`/rag/retrieve` 用，零模型调用）；
* ``optimize_for_rewrite``：先改写，再对改写句做同义词扩展（`/rag/rewrite` 用，1 次模型
  调用）。改写失败按 ``50003`` 上抛，不在这里吞。

**顺序不许反**：改写补语义、同义词补字面。先扩展后改写，同义词表就只能匹配用户原话；
而改写恰恰会把口语换成文档里那种说法（「退钱」→「退款」），词表里配的往往是后者。

谁用哪一句（这一句是本模块存在的理由）：

* ``vector_queries`` → 向量一路**都搜**（改写端点里是原句 + 改写句，两句合成一个候选池）；
* ``effective_query`` → **重排**（语义那一侧，含"给模型看的原始问题"）；
* ``search_query`` → 关键词一路（字面那一侧，同义词补的就是它）。

**改写不再是「替换」而是「并集」**：原句与改写句都查一次向量、合成一个候选池再进既有
RRF，谁的那份好就留谁；关键词一路与重排拿的句子一律不变。
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
    """一次查询加工的结果：**两路各自的查询** + 这次实际用了哪些句子 + 耗时。

    ``effective_query`` 与 ``search_query`` 不同是**设计**，不是 bug：同义词补的是字面
    匹配，对语义召回没有意义；改写补的是语义，对字面匹配没好处。硬把它们合成一句，
    等于用其中一路的优化去污染另一路。

    ``rewrite_applied`` 是这次加工**真的发生了一次改写**的唯一标记：它进
    ``metadata.retrieval.query_optimization``，评测里用它区分「名次变了是因为检索不行，
    还是因为改写把问题改坏了」。
    """

    raw_query: str
    effective_query: str
    search_query: str
    vector_queries: tuple[str, ...]
    rewrite_applied: bool
    duration_ms: float


def direct_query(query: str) -> QueryProcessResult:
    """恒等变换：三句都等于原句。``/rag/retrieve`` 用它，一次模型都不调。"""

    return QueryProcessResult(
        raw_query=query,
        effective_query=query,
        search_query=query,
        vector_queries=(query,),
        rewrite_applied=False,
        duration_ms=0.0,
    )


async def optimize_for_rewrite(
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
    effective_query = rewrite.query

    if settings.SYNONYM_EXPANSION_ENABLED:
        # 匹配的是 ``effective_query``：改写之后的句子才是关键词一路要用的那一句。
        search_query = expand_synonyms(effective_query, synonym_groups)
    else:
        search_query = effective_query

    # 两句都搜，顺序固定为「原句 → 改写句」，两句相同时去重成一句。
    vector_queries = tuple(
        dict.fromkeys(text for text in (query, effective_query) if text.strip())
    )

    return QueryProcessResult(
        raw_query=query,
        effective_query=effective_query,
        search_query=search_query,
        vector_queries=vector_queries,
        rewrite_applied=True,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )


__all__ = ["QueryProcessResult", "direct_query", "optimize_for_rewrite"]
