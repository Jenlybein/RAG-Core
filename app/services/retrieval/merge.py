"""多句召回的并集规则：**两路共用一份**（#31 起是向量，第 15 步起关键词也用）。

为什么要单独一个模块：向量一路（``vector_recall``）与关键词一路（``pipeline._keyword_branch``）
都要做同一件事——「同一个查询里查了几句」折叠成一路结果，而它们对这件事的约定必须**完全一致**。
放在各自文件里，早晚会出现两套去重规则：一套「分数取更高」，另一套「保留先来的」，
而差别不会报错，只会让同一批切片在两条路上的名次不一样。

**这不是再嵌一层 RRF**：这里只把同一路里的多次查询折成一路，名次 / 分数字段仍由
``fuse_by_rrf``（或单模式组装层）按老口径补。融合函数一行未动。
"""

from __future__ import annotations

from typing import Any


def _score(row: dict) -> float:
    return float(row.get("score") or 0.0)


def merge_rows(ranked_batches: list[list[dict]]) -> list[dict]:
    """把每次查询各自的召回列表并成**一个**池：唯一键 ``(kb_id, chunk_id)``。

    同一命中取「更好」的一次有两层意思：

    * **名次**：保留 rank 更小的那份整行（content / title 等跟着名次走）；
    * **分数**：取两次里更高者（**不求和**——两次查询的分数来自不同的检索式，
      相加会让「两次都勉强命中」压过「一次明确命中」，那不是我们想要的并集）。

    去重后按分数降序、``kb_id`` / ``chunk_id`` 兜底排序，顺序不随查询先后漂移。
    单批次时直接返回原列表的浅拷贝、**不排序**：两条调用方历史上各有自己的排序口径
    （见 :func:`app.services.retrieval.vector_recall.vector_recall`），这里一排序就会
    悄悄改变「只有一句」时的同分顺序。

    ``kb_id`` 允许为 ``None``（向量库的行理论上有，但旧数据不一定有）：键用
    ``(row.get("kb_id"), chunk_id)``，两个 ``None`` 的库 id 会互相覆盖——这只在
    「同一 chunk_id 出现在两个没有 kb_id 的库里」时发生，现实中不存在（chunk id 全局唯一）。
    """

    if len(ranked_batches) == 1:
        return list(ranked_batches[0])

    merged: dict[Any, dict] = {}
    best_rank: dict[Any, int] = {}
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


__all__ = ["merge_rows"]
