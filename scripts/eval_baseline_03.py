"""最终版公平对照。

两个必须修掉的测量错误：
1. 用 heading_path 精确相等判相关 → 03 时代路径更粗，15 道难题被静默剔除；
2. 行级表片的文本是拼出来的（`参数=…，值=…`），在原文里 `find` 不到，字符区间
   是错的 → 它明明答对了也不算命中。

这版：统一用文本区间重叠（与朴素切块同判据）、行级片继承所属表格区间、
两版都在 127 道题上算。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "scripts")

from app.providers.embedding import get_embedding_provider
from app.utils import markdown_splitter as ms
from app.utils.retrieval_text import build_retrieval_text
from eval_chunking import (
    DOMINANCE_RATIO,
    EXCLUDED_FROM_MAIN,
    _cosine,
    _load_corpus,
    _load_questions,
    _sections,
)
from eval_metrics import Case, aggregate

K = 5


class _Chunk:
    __slots__ = ("text", "doc_stem", "heading_path", "chunk_type", "start", "end")

    def __init__(self, text, doc_stem, heading, start, end, kind):
        self.text = text
        self.doc_stem = doc_stem
        self.heading_path = heading
        self.start = start
        self.end = end
        self.chunk_type = kind


def _cut(doc, *, level: int, rows: bool, model_tags: bool, originals):
    ms._table_row_texts = originals[0]
    ms.MarkdownStructuredSplitter._split_code_lines = originals[1]
    ms.MarkdownStructuredSplitter.SECTION_LEVEL = level
    if not rows:
        ms._table_row_texts = lambda _g: []
    if not model_tags:
        ms.MarkdownStructuredSplitter._split_code_lines = lambda self, lines: [
            "\n".join(lines).rstrip()
        ]

    chunks: list[_Chunk] = []
    cursor = 0
    last_table: tuple[int, int] = (0, 0)
    for piece in ms.MarkdownStructuredSplitter(800, 100, 10).split(doc.text):
        kind = str(piece.metadata.get("chunk_type") or "")
        if kind == "table_row":
            # 行级片的正文是按列拼出来的，原文里搜不到；它属于同一张表，
            # 所以直接继承上一张整表片的区间。
            start, end = last_table
        else:
            position = doc.text.find(piece.text, cursor)
            if position < 0:
                position = doc.text.find(piece.text)
            if position < 0:
                position = cursor
            start = position
            end = position + len(piece.text)
            cursor = end
            if kind == "table":
                last_table = (start, end)
        chunks.append(
            _Chunk(
                piece.text,
                doc.stem,
                str(piece.metadata.get("heading_path") or ""),
                start,
                end,
                kind,
            )
        )
    ms._table_row_texts = originals[0]
    ms.MarkdownStructuredSplitter._split_code_lines = originals[1]
    ms.MarkdownStructuredSplitter.SECTION_LEVEL = originals[2]
    return chunks


async def _run() -> None:
    docs = _load_corpus()
    questions, _ = _load_questions()
    questions = [q for q in questions if q.labels and q.type not in EXCLUDED_FROM_MAIN]
    provider = get_embedding_provider()
    section_map = {(d.stem, s.heading_path): s for d in docs for s in _sections(d.text)}

    originals = (
        ms._table_row_texts,
        ms.MarkdownStructuredSplitter._split_code_lines,
        ms.MarkdownStructuredSplitter.SECTION_LEVEL,
    )
    variants = {
        "old": ("03 时代（h1/h2，整表整码）", dict(level=2, rows=False, model_tags=False)),
        "new": (
            "现在（h3+行级表+代码切+型号段）",
            dict(level=3, rows=True, model_tags=True),
        ),
    }

    per_type_all: dict[str, dict[str, list[bool]]] = {}
    for key, (label, cfg) in variants.items():
        chunks = [c for doc in docs for c in _cut(doc, originals=originals, **cfg)]
        ids = [str(i) for i in range(len(chunks))]
        by_id = dict(zip(ids, chunks))
        texts = [
            build_retrieval_text(
                next(d.title for d in docs if d.stem == c.doc_stem),
                c.heading_path,
                c.text,
                model_tags=cfg["model_tags"],
            )
            for c in chunks
        ]
        vectors = await provider.embed_texts(texts)
        query_vectors = await provider.embed_texts([q.query for q in questions])

        cases = []
        per_type: dict[str, list[bool]] = {}
        no_rel = 0
        for question, qv in zip(questions, query_vectors):
            relevant = set()
            for cid in ids:
                chunk = by_id[cid]
                for gold in question.labels:
                    section = section_map.get(gold)
                    if section is None or chunk.doc_stem != gold[0]:
                        continue
                    overlap = min(chunk.end, section.end) - max(chunk.start, section.start)
                    if overlap > 0 and overlap >= DOMINANCE_RATIO * min(
                        len(chunk.text), section.end - section.start
                    ):
                        relevant.add(cid)
                        break
            if not relevant:
                no_rel += 1
                continue
            ranked = [
                cid
                for _s, cid in sorted(
                    ((_cosine(qv, vec), cid) for vec, cid in zip(vectors, ids)),
                    reverse=True,
                )
            ]
            cases.append(
                Case(
                    id=question.id,
                    type=question.type,
                    relevant=frozenset(relevant),
                    returned=tuple(ranked),
                )
            )
            per_type.setdefault(question.type, []).append(ranked[0] in relevant)

        m = aggregate(cases, K)
        n = sum(len(c.relevant) for c in cases) / len(cases)
        per_type_all[key] = per_type
        print(
            f"{label}\n"
            f"  片数={len(chunks)} 均长={sum(len(c.text) for c in chunks) / len(chunks):.0f}"
            f" 参评={len(cases)} 无相关={no_rel} N={n:.2f}\n"
            f"  R@5={float(m['recall_at_k']):.4f} R@1={float(m['recall_at_1']):.4f}"
            f" hit@1={float(m['hit_at_1']):.4f} MRR={float(m['mrr']):.4f}"
        )

    a, b = per_type_all["old"], per_type_all["new"]
    print("\n按题型 hit@1（同判据、同 127 题）")
    print(f"{'题型':18s} {'题数':>4} {'03':>7} {'现在':>7} {'差值':>8}")
    for qtype in sorted(set(a) | set(b), key=lambda t: -(_r(b, t) - _r(a, t))):
        print(
            f"{qtype:18s} {len(a.get(qtype, [])):4d} {_r(a, qtype):7.3f}"
            f" {_r(b, qtype):7.3f} {_r(b, qtype) - _r(a, qtype):+8.3f}"
        )


def _r(side: dict[str, list[bool]], qtype: str) -> float:
    flags = side.get(qtype) or []
    return sum(flags) / len(flags) if flags else 0.0


if __name__ == "__main__":
    asyncio.run(_run())
