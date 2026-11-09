"""向量一路的「多查询并集」（#31）：逐句召回 → 并成一个池 → 交给既有 RRF。

**不是再嵌一层 RRF**：这里只把「同一路里查了几次」折叠成一路结果，名次 / 分数
字段仍由 ``fuse_by_rrf`` 或单模式组装层按老口径补。融合函数一行未改。

为什么单独一个模块：``pipeline._vector_branch``（独立线程 + 独立 Session）与
``rag_service._retrieve_by_vector``（单模式）都要做同一件事，而它们对该有的行为
约定必须完全一致——尤其「关闭时逐字节不变」。放在同一处，才不会出现两条路径
一套去重规则。

成本说明（**查询侧没有批量接口**）：``EmbeddingProvider`` 只有
``embed_query(query: str) -> list[float]``（单句、低延迟）与
``embed_texts(texts: list[str])``（**文档侧**批量，LangChain 下走
``aembed_documents``，与查询侧 ``aembed_query`` 不是同一条路径，不能拿来混用）。
两句因此是 **2 次 embed_query + 2 次 similarity_search_scope**，嵌入用
``asyncio.gather`` 并发发出：约 1 次 RTT，但配额仍是 2 次。关闭 / 改写失败时
只有一句，仍是 1 次嵌入 + 1 次向量查询，调用次数与旧实现完全相同。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from app.models.chunk import default_index_versions


def _score(row: dict) -> float:
    return float(row.get("score") or 0.0)


def _merge_rows(ranked_batches: list[list[dict]]) -> list[dict]:
    """把每句各自的召回列表并成**一个**池：唯一键 ``(kb_id, chunk_id)``。

    同一命中取「更好」的一次有两层意思：

    * **名次**：保留 rank 更小的那份整行（content / title 等跟着名次走）；
    * **分数**：取两次里更高者（不同查询的相似度可以比一比谁更有把握）。

    去重后按分数降序、``kb_id`` / ``chunk_id`` 兜底排序，顺序不随查询先后漂移。
    单批次时直接返回原列表的浅拷贝、**不排序**：两条调用方历史上各有自己的
    排序口径（见 :func:`vector_recall`），这里一排序就会悄悄改变关闭时的同分顺序。
    """

    if len(ranked_batches) == 1:
        return list(ranked_batches[0])

    merged: dict[tuple[str | None, str], dict] = {}
    best_rank: dict[tuple[str | None, str], int] = {}
    for rows in ranked_batches:
        for rank, row in enumerate(rows, start=1):
            key = (row.get("kb_id"), str(row["chunk_id"]))
            entry = merged.get(key)
            if entry is None:
                merged[key] = dict(row)
                best_rank[key] = rank
                continue
            # 分数取更高者；整行只在名次更好时替换，避免把好名次的内容丢掉。
            entry["score"] = max(_score(entry), _score(row))
            if rank < best_rank[key]:
                best_rank[key] = rank
                row_score = entry["score"]
                merged[key] = dict(row)
                merged[key]["score"] = row_score

    return sorted(
        merged.values(),
        key=lambda item: (-_score(item), item.get("kb_id") or "", item["chunk_id"]),
    )


async def vector_recall(
    *,
    db: Any,
    tenant_id: str,
    kb_ids: list[str],
    queries: Sequence[str],
    top_k: int,
    embedding_factory: Any,
    vector_factory: Any,
    single_query_order: str = "stable",
) -> list[dict]:
    """每句各嵌一次、各做一次范围召回，并成一个池后截到 ``top_k``。

    单句时**仍走旧的快路径**：一次 ``embed_query``、一次 ``similarity_search_scope``，
    调用次数与逐字节响应都不变。排序口径由 ``single_query_order`` 选，因为历史上
    两条调用方并不相同，统一口径会在同分多行时悄悄改变关闭时的结果：

    * ``"stable"``（``mode=vector`` 单模式）：``rows.sort(score, reverse=True)``，
      稳定排序，同分保留库返回顺序；
    * ``"tiebreak"``（混合向量分支）：``(-score, kb_id, chunk_id)`` 显式兜底。

    多句时嵌入并发发出，向量查询也并发发出（都是异步原语）；
    ``asyncio.gather`` 默认 ``return_exceptions=False``：任一句的嵌入 / 查询失败
    就是这一路失败，由上层按既有规则降级（混合）或硬失败（单模式），
    绝不拿「另一句的半份结果」冒充完整向量池。
    """

    queries = [query for query in queries if query.strip()]
    if not queries:
        return []

    index_versions = default_index_versions(kb_ids)
    provider = embedding_factory()
    store = vector_factory()

    vectors = await asyncio.gather(*(provider.embed_query(query) for query in queries))
    batches = await asyncio.gather(
        *(
            store.similarity_search_scope(
                db,
                query_vector,
                tenant_id=tenant_id,
                # 映射而不是两个平行数组：配对关系只能从映射里读出来。
                index_versions=index_versions,
                top_k=top_k,
            )
            for query_vector in vectors
        )
    )
    ranked_batches = [list(batch) for batch in batches]

    if len(ranked_batches) == 1:
        rows = ranked_batches[0]
        if single_query_order == "tiebreak":
            # 混合分支的历史口径：同分时按 kb_id / chunk_id 排，名次不随插入顺序漂移。
            rows.sort(
                key=lambda item: (
                    -item.get("score", 0.0),
                    item.get("kb_id") or "",
                    item["chunk_id"],
                )
            )
        else:
            # 单模式的历史口径：稳定排序，同分保留库返回顺序。
            rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return rows[:top_k]

    return _merge_rows(ranked_batches)[:top_k]
