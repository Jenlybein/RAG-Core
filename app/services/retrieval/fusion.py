"""检索编排的纯函数部分：RRF 融合。

**这个模块不碰数据库、不碰网络**：输入是两路召回的切片列表，输出是一个有序列表。
这样「融合分与手算一致」「同分顺序可复现」这两件事都能在离线单测里钉死，
不必启动应用、不必连 PostgreSQL（见 tests/test_rrf_fusion.py）。

公式（父 spec「融合契约」）：``score(d) = 1/(k + 向量名次) + 1/(k + 关键词名次)``，
某一路未召回该文档时该项不累加。``k=60`` 写死——它一旦可调，同一档位在不同调用方
那里就是不同的检索链路，评测与复现都做不了。

三件「不属于算法、属于可复现性」的约定，全部落在本模块：

* 唯一键是 ``(kb_id, chunk_id)`` 而不是 ``chunk_id``：跨库同名切片是两条结果；
* 名次是**该路召回列表里的位置**（从 1 开始），不是分数排序后的位置；
* 同分必须有确定性 tie-breaker：融合分降序 → 双路命中优先 → 两路中最佳名次 →
  ``kb_id`` → ``chunk_id``。没有它，同分顺序就取决于哪一路先返回。
"""

from __future__ import annotations

from typing import Any

from app.utils.retrieval_fields import with_score_columns

# 融合常数：服务端写死，绝不进请求体（父 spec「融合契约」）。
DEFAULT_RRF_K = 60


def fuse_by_rrf(
    vector_chunks: list[dict],
    bm25_chunks: list[dict],
    *,
    rrf_k: int = DEFAULT_RRF_K,
    top_n: int,
) -> list[dict]:
    """把两路召回揉成一个有序列表；纯函数，同样输入必得同样输出。

    ``rrf_k`` 只是给本模块的单测留的口子（验证「k 变大时相邻名次差距被压平」这类
    性质），**生产路径永远不传它**：``pipeline`` 调用时不带该参数，请求体里也没有
    这个字段。换句话说，`k=60` 的写死是靠「没有调用方能覆盖它」保证的，不是靠签名。

    每路切片只需 ``kb_id`` / ``chunk_id`` / ``document_id`` / ``score``，其余字段
    （``title`` / ``content``）原样带给调用方。返回的每条都补齐五个分列字段
    （见 ``app.utils.retrieval_fields.with_score_columns``）：``vector_score`` /
    ``bm25_score`` / ``vector_rank`` / ``bm25_rank`` / ``rrf_score``，未命中的那一路
    是 ``None`` 而不是 0。
    """

    if rrf_k <= 0:
        # 分母必须为正：k 是「让靠后名次衰减得慢一点」的平滑量，不是业务参数。
        raise ValueError(f"rrf_k 必须为正数，当前是 {rrf_k!r}")

    merged: dict[tuple[str | None, str], dict] = {}

    for chunks, prefix in ((vector_chunks, "vector"), (bm25_chunks, "bm25")):
        for rank, chunk in enumerate(chunks, start=1):
            key: tuple[str | None, str] = (chunk.get("kb_id"), str(chunk["chunk_id"]))
            entry = merged.get(key)
            if entry is None:
                # 五列字段由 ``with_score_columns`` 一次性补齐（rrf 从 0.0 开始累加）。
                entry = with_score_columns(
                    {
                        "document_id": chunk["document_id"],
                        "chunk_id": chunk["chunk_id"],
                        "kb_id": chunk.get("kb_id"),
                        "title": chunk.get("title", ""),
                        "content": chunk.get("content", ""),
                    },
                    rrf_score=0.0,
                )
                merged[key] = entry
            entry[f"{prefix}_rank"] = rank
            entry[f"{prefix}_score"] = chunk.get("score")
            entry["rrf_score"] += 1.0 / (rrf_k + rank)

    ordered = sorted(merged.values(), key=_fusion_sort_key)
    return [{**entry, "score": entry["rrf_score"]} for entry in ordered[:top_n]]


def _fusion_sort_key(entry: dict[str, Any]) -> tuple:
    """确定性排序键，顺序即契约：

    融合分降序 → 双路命中优先 → 两路中最佳名次 → ``kb_id`` → ``chunk_id``。

    前三条决定「谁更相关」，后两条只负责「一样相关时谁在前面」。因为
    ``1/(k+rank)`` 在固定 k 下是严格凸的，真正同分的两条必然名次多重集相同，
    所以第二、三条只在浮点碰撞时才会真正发力；即便如此，顺序也必须写死——
    这正是父 spec 要求「同分有确定性 tie-breaker」的原因。
    """

    ranks = [rank for rank in (entry["vector_rank"], entry["bm25_rank"]) if rank is not None]
    return (
        -entry["rrf_score"],
        -len(ranks),
        min(ranks),
        entry["kb_id"] or "",
        entry["chunk_id"],
    )
