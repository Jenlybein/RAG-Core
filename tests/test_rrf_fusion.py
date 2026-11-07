"""RRF 融合：本步最值钱的离线测试（纯函数，手算能对得上）。

接缝：只调 ``app.services.retrieval.fusion.fuse_by_rrf``，不碰数据库、不碰网络、
不启动应用。断言的期望值一律**手算**，不是用被测函数的公式再算一遍。

真值表（``k=60``，即分母 61 / 62 / 63）：

| 切片 | 向量名次 | 关键词名次 | 融合分                       | 最终名次 |
| --- | --- | --- | --- | --- |
| A | 1 | 1 | 1/61 + 1/61 = 0.03278688… | 1 |
| C | 3 | 2 | 1/63 + 1/62 = 0.03200204… | 2 |
| B | 2 | — | 1/62        = 0.01612903… | 3 |

``B`` 只在向量一路命中，所以它的关键词分与关键词名次是 ``null`` 而不是 0：
「没命中」与「命中但分数为 0」必须可区分（响应契约）。
"""

from __future__ import annotations

import pytest

from app.services.retrieval.fusion import fuse_by_rrf

KB_A = "kb-a"
KB_B = "kb-b"


def _chunk(*, kb_id: str = KB_A, chunk_id: str, score: float, document_id: str | None = None):
    """一路召回的原始切片（两路同形状，谁也不需要知道对方的存在）。"""

    return {
        "document_id": document_id or f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "kb_id": kb_id,
        "title": f"title-{chunk_id}",
        "content": f"content-{chunk_id}",
        "score": score,
    }


def _ids(fused: list[dict]) -> list[str]:
    return [item["chunk_id"] for item in fused]


# ───────────────────────────────────────────────────── 真值表：分与名次


def test_rrf_truth_table_matches_hand_computed_scores() -> None:
    """三行真值表：融合分与最终名次都按公式手算得出。"""

    vector_chunks = [
        _chunk(chunk_id="A", score=0.93),  # 向量第 1 名
        _chunk(chunk_id="B", score=0.71),  # 向量第 2 名
        _chunk(chunk_id="C", score=0.62),  # 向量第 3 名
    ]
    bm25_chunks = [
        _chunk(chunk_id="A", score=12.4),  # 关键词第 1 名
        _chunk(chunk_id="C", score=9.1),  # 关键词第 2 名
    ]

    fused = fuse_by_rrf(vector_chunks, bm25_chunks, top_n=10)

    assert _ids(fused) == ["A", "C", "B"]
    assert [item["rrf_score"] for item in fused] == pytest.approx(
        [
            0.0327868852459016,  # 1/61 + 1/61
            0.0320020481310804,  # 1/63 + 1/62
            0.0161290322580645,  # 1/62
        ]
    )
    # 名次就是「本路召回列表里的位置」，从 1 开始；不是分数排序后的位置。
    assert [(item["vector_rank"], item["bm25_rank"]) for item in fused] == [
        (1, 1),
        (3, 2),
        (2, None),
    ]
    # 两路原始分原样保留，口径不同也照样并列展示。
    assert [(item["vector_score"], item["bm25_score"]) for item in fused] == [
        (0.93, 12.4),
        (0.62, 9.1),
        (0.71, None),
    ]


def test_score_is_the_fusion_score_in_hybrid_mode() -> None:
    """``score`` 的语义是「本次最终排序分」：混合模式下就是融合分。"""

    fused = fuse_by_rrf(
        [_chunk(chunk_id="A", score=0.9)],
        [_chunk(chunk_id="B", score=12.5)],
        top_n=10,
    )

    assert [item["score"] for item in fused] == [item["rrf_score"] for item in fused]


def test_missing_channel_keeps_null_not_zero() -> None:
    """只命中一路时，另一路的分数与名次是 ``null``。"""

    fused = fuse_by_rrf([], [_chunk(chunk_id="hit", score=12.5)], top_n=10)

    assert len(fused) == 1
    item = fused[0]
    assert item["vector_score"] is None
    assert item["vector_rank"] is None
    assert item["bm25_score"] == 12.5
    assert item["bm25_rank"] == 1


def test_slice_fields_are_carried_through() -> None:
    """回给调用方的仍是切片原文与出处，融合不重写它们。"""

    fused = fuse_by_rrf([_chunk(chunk_id="A", score=0.9)], [], top_n=10)

    assert fused[0]["document_id"] == "doc-A"
    assert fused[0]["title"] == "title-A"
    assert fused[0]["content"] == "content-A"
    assert fused[0]["kb_id"] == KB_A


# ───────────────────────────────────────── 唯一键：(kb_id, chunk_id)


def test_unique_key_is_kb_id_plus_chunk_id() -> None:
    """不同库里的同名 chunk_id 是两条切片，绝不能被合并成一条。"""

    fused = fuse_by_rrf(
        [_chunk(kb_id=KB_A, chunk_id="c1", score=0.9)],
        [_chunk(kb_id=KB_B, chunk_id="c1", score=12.5)],
        top_n=10,
    )

    assert [(item["kb_id"], item["chunk_id"]) for item in fused] == [
        (KB_A, "c1"),
        (KB_B, "c1"),
    ]
    assert [item["vector_rank"] for item in fused] == [1, None]
    assert [item["bm25_rank"] for item in fused] == [None, 1]


# ─────────────────────────────────────────── 确定性 tie-breaker


def test_same_input_returns_the_same_order_twice() -> None:
    """同样输入两次结果完全相同——评测可复现的前提，含同分场景。"""

    vector_chunks = [
        _chunk(kb_id=KB_B, chunk_id="b1", score=0.50),
        _chunk(kb_id=KB_A, chunk_id="a1", score=0.50),
    ]
    bm25_chunks = [_chunk(kb_id=KB_A, chunk_id="a1", score=0.0)]

    first = fuse_by_rrf(vector_chunks, bm25_chunks, top_n=10)
    second = fuse_by_rrf(vector_chunks, bm25_chunks, top_n=10)

    assert first == second
    assert _ids(first) == ["a1", "b1"]


def test_ties_break_on_kb_id_then_chunk_id() -> None:
    """融合分相同（同为 1/61）时，名次也相同，最后靠 kb_id、chunk_id 兜底。"""

    # kb-2 是向量第 1 名（1/61），kb-1 是关键词第 1 名（1/61）——真正的同分。
    # 两条都只命中一路、最佳名次都是 1，于是 kb_id 决定先后。
    fused = fuse_by_rrf(
        [
            _chunk(kb_id="kb-2", chunk_id="z", score=0.4),
            _chunk(kb_id="kb-3", chunk_id="z", score=0.4),
        ],
        [_chunk(kb_id="kb-1", chunk_id="z", score=7.0)],
        top_n=10,
    )

    # kb-1 与 kb-2 同分（1/61）→ kb-1 在前；kb-3 是向量第 2 名（1/62），排最后。
    assert [item["kb_id"] for item in fused] == ["kb-1", "kb-2", "kb-3"]


def test_same_fusion_score_in_one_kb_breaks_on_chunk_id() -> None:
    """同库同分：``chunk_id`` 兜底，顺序与输入顺序无关。"""

    vector_chunks = [
        _chunk(chunk_id="b", score=0.40),
        _chunk(chunk_id="a", score=0.30),
    ]

    forward = fuse_by_rrf(vector_chunks, [], top_n=10)
    reversed_input = fuse_by_rrf(list(reversed(vector_chunks)), [], top_n=10)

    assert _ids(forward) == ["b", "a"]
    # 反过来喂，名次也反了，融合分跟着反——这是 RRF 的语义，不是不确定性。
    assert _ids(reversed_input) == ["a", "b"]

    # 真正的同分（名次相同）只能来自同一名次：两路各自第 1 名。
    tie = fuse_by_rrf(
        [_chunk(kb_id=KB_B, chunk_id="zz", score=0.9)],
        [_chunk(kb_id=KB_A, chunk_id="yy", score=9.0)],
        top_n=10,
    )
    assert [(item["kb_id"], item["chunk_id"]) for item in tie] == [
        (KB_A, "yy"),
        (KB_B, "zz"),
    ]


# ────────────────────────────────────────────────── top_n 与 k


def test_top_n_truncates_after_fusion() -> None:
    vector_chunks = [_chunk(kb_id=KB_A, chunk_id=f"v{i}", score=1.0 - i / 10) for i in range(1, 6)]

    fused = fuse_by_rrf(vector_chunks, [], top_n=2)

    assert _ids(fused) == ["v1", "v2"]


def test_default_k_is_60() -> None:
    """``k=60`` 写死：单独一条两路都第 1 名，融合分必须正好是 2/61。"""

    fused = fuse_by_rrf(
        [_chunk(chunk_id="A", score=0.9)],
        [_chunk(chunk_id="A", score=12.5)],
        top_n=10,
    )

    assert fused[0]["rrf_score"] == pytest.approx(0.0327868852459016)  # 2/61


def test_empty_inputs_produce_empty_output() -> None:
    assert fuse_by_rrf([], [], top_n=5) == []
