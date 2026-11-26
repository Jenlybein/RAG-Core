"""问题分解后检索：把 N 条子查询的内核结果合成**一份**交付单元列表。

第 13 步新增的 `/rag/decompose` 与「改写」的关键差别就在这里：改写是**同一个问题的两种
说法**，两句向量召回可以并成一个池、去重取优；分解是**几个不同的问题**，一个池会让
A 子问题的高分命中把 B 子问题的唯一命中挤掉——而「别漏」正是分解存在的理由。

所以合并按**每条子问题各保底一个名额**，再按子问题顺序轮转填满 ``top_k``：

1. 每条子查询的结果先按自己的顺序保序（内核已经排好，这里不重算分数）；
2. 按交付单元身份去重（优先 ``section_id``，没有时退到 ``chunk_id``）；
3. 第 1 轮：每条子查询各交出自己的第 1 条（**保底**）；
4. 之后各轮：按同样的子查询顺序，每条再交出下一条，直到 ``top_k`` 填满或都没料；
5. 每条结果补 ``matched_queries``：所有把它顶上来的子查询原句，按子查询顺序。

**保底优先于条数上限**：子问题有 4 条而 ``top_k=2`` 时，返回的是 4 条而不是 2 条。这不是
越界，而是这条端点唯一的存在理由——「别漏」比「别多」重要：把 B 子问题唯一的命中砍掉，
调用方拿到的就是一份看起来完整、其实只有一个子问题被回答了的列表。去重之后如果多条子
问题命中同一个交付单元，条数自然会收敛，所以「保底」不会真的放大结果集，它只是不让某条
子问题的最后一根线索被名额挤掉。

**跨子问题的 ``score`` 不可比**：合并函数不重算分数、不重新排序，只保留代表片
（第一次命中它的那条子查询）的分列字段。调用方要判断「哪一路命中、排第几」只能读
``vector_score`` / ``bm25_score`` / ``rrf_score``，不能拿 ``score`` 当跨问题的阈值。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 交付单元的身份键（与受控 Research 的 ``material_key`` 同一口径）。
_IDENTITY_KEYS: tuple[str, ...] = ("section_id", "chunk_id")


def _identity_of(row: Mapping[str, Any]) -> str | None:
    for key in _IDENTITY_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def merge_sub_query_results(
    results: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """把 ``[(子查询原句, 该子查询的交付单元列表), ...]`` 合成一份列表。

    纯函数：不碰数据库、不读配置、不调模型。``top_k <= 0`` 或没有任何有身份的行时
    返回空列表。结果条数**通常**不超过 ``top_k``，唯一例外是保底轮：子问题条数多于
    ``top_k`` 时保底优先（理由见模块文档），此时条数 = 各子问题首条去重后的条数。
    """

    if top_k <= 0:
        return []

    # 每条子查询先按自己的顺序留一个有身份的序列；空身份的行一律丢弃。
    streams: list[tuple[str, list[Mapping[str, Any]]]] = [
        (query, [row for row in rows if _identity_of(row) is not None]) for query, rows in results
    ]

    delivered: dict[str, dict[str, Any]] = {}
    matched: dict[str, list[str]] = {}

    def _take(query: str, row: Mapping[str, Any], *, cap: int | None) -> bool:
        """把这一条纳入交付；已交付时只补 ``matched_queries``，不重复占位。

        ``cap=None`` 表示这一轮不受条数上限约束（保底轮）。
        """

        identity = _identity_of(row)
        if identity is None:
            return False
        if identity in delivered:
            if query not in matched[identity]:
                matched[identity].append(query)
            return False
        if cap is not None and len(delivered) >= cap:
            return False
        delivered[identity] = dict(row)
        matched[identity] = [query]
        return True

    # 每条子查询一个独立指针：轮转时反复从各流的「下一条未消费行」取。
    # 指针不能共用一个全局 cursor——撞到重复行时那条流只推进自己的指针、不占全局名额，
    # 共用 cursor 会让下一条可用行被跳过（实测踩过）。
    cursors = [0 for _ in streams]

    # 第 1 轮：保底。每条子查询交出自己的第 1 条，**不受 top_k 约束**——「每个子问题
    # 至少有一条」是这条端点的承诺，条数上限在这一轮之后才生效。
    for index, (query, rows) in enumerate(streams):
        if rows:
            _take(query, rows[0], cap=None)
            cursors[index] = 1

    # 之后各轮：按子查询顺序轮转，把剩下的名额填到 top_k。
    while len(delivered) < top_k:
        advanced = False
        for index, (query, rows) in enumerate(streams):
            cursor = cursors[index]
            if cursor >= len(rows):
                continue
            cursors[index] = cursor + 1
            _take(query, rows[cursor], cap=top_k)
            advanced = True
            if len(delivered) >= top_k:
                break
        if not advanced:
            break

    result: list[dict[str, Any]] = []
    for identity, row in delivered.items():
        row["matched_queries"] = list(matched[identity])
        result.append(row)
    return result


__all__ = ["merge_sub_query_results"]
