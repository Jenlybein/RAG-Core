"""查询加工的编排入口：把用户那句话加工成「召回用几句 + 归因用一句」。

第 13 步起有两种调用形态，由**端点**决定，不再由密钥配置里的开关决定：

* ``direct_query``：恒等变换（`/rag/retrieve` 用，零模型调用）；
* ``optimize_for_rewrite``：调一次模型做改写（`/rag/rewrite` 用，1 次模型调用）。
  改写失败按 ``50003`` 上抛，不在这里吞。

谁用哪一句（这一句是本模块存在的理由）：

* ``recall_queries`` → **两路召回都搜**（改写端点里是原句 + 改写句；向量路逐句嵌、
  关键词路逐句查 ——— 第 15 步起两条路形状完全一致）；
* 重排 → **原句**（不在本模块的产物里：重排与改写无关，见 ``rag_service``）；
* ``effective_query`` → **只进响应 metadata 供归因**（「这次改写说了什么」），
  内部没有任何消费者。

**改写是「并集」而不是「替换」**：原句与改写句都搜一次，各自的那份命中合成一个候选池
再进既有 RRF，谁的那份好就留谁。改写把口语换成文档里的说法（「退钱」→「退款」）时，
两者各能捞到对方捞不到的切片，这正是并集的理由。

**同义词扩展已下线**（第 15 步）：词表是库级人工配置，覆盖面与维护成本都不划算；
字面那一路现在吃的是「原句 + 改写句」这两句真实存在的话，不再有第三句「扩展出来的」句子。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.services.query.rewrite import rewrite_query


@dataclass(frozen=True)
class QueryProcessResult:
    """一次查询加工的结果：**召回用哪几句** + **改写句（归因用）** + 是否真改写过 + 耗时。

    ``recall_queries`` 是两条召回通道共用的那一组句子，顺序固定为「原句 → 改写句」，
    两句相同时去重成一句（字符串完全相同才去重，不做语义去重：那要再引一个阈值，
    而且「看起来一样」与「真的相同」是两件事）。

    ``effective_query`` 与召回用的句子可能相同（改写器原句返回时）：它在这里不是为了
    召回，而是为了让人们在事后回答「这次模型把问题改成了什么」——q16 那次退化当年正是
    因为答不出这个问题而无法归因。

    ``rewrite_applied`` 是这次加工**真的发生了一次改写**的唯一标记：它进
    ``metadata.retrieval.query_optimization``，评测里用它区分「名次变了是因为检索不行，
    还是因为改写把问题改坏了」。
    """

    raw_query: str
    effective_query: str
    recall_queries: tuple[str, ...]
    rewrite_applied: bool
    duration_ms: float


def direct_query(query: str) -> QueryProcessResult:
    """恒等变换：用句就是原句。``/rag/retrieve`` 用它，一次模型都不调。"""

    return QueryProcessResult(
        raw_query=query,
        effective_query=query,
        recall_queries=(query,),
        rewrite_applied=False,
        duration_ms=0.0,
    )


async def optimize_for_rewrite(
    *,
    query: str,
    rewrite_hint: str | None = None,
) -> QueryProcessResult:
    """改写一次 → 返回「原句 + 改写句」这一组召回用句。

    ``rewrite_hint`` 由调用方传进来：本模块不查库、不认识 ``KnowledgeBase``。多库检索时
    调用方在可见性预校验那一次已经把库行拿在手上，这里再查一次库就会变成「每个库两次查询」。
    """

    started = time.perf_counter()
    rewrite = await rewrite_query(query=query, hint=rewrite_hint)
    effective_query = rewrite.query

    recall_queries = tuple(
        dict.fromkeys(text for text in (query, effective_query) if text.strip())
    )

    return QueryProcessResult(
        raw_query=query,
        effective_query=effective_query,
        recall_queries=recall_queries,
        rewrite_applied=True,
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )


__all__ = ["QueryProcessResult", "direct_query", "optimize_for_rewrite"]
