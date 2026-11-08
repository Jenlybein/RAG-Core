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
from app.services import result_collapse
from app.services.result_collapse import OVERFETCH_FACTOR
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
    overall: dict[str, tuple] = {}
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
                # 必须传类型：行级表片不加型号段这条规则在 build_retrieval_text 里，
                # 不传就等于把规则绕过，量出来的不是生产口径。
                chunk_type=c.chunk_type,
            )
            for c in chunks
        ]
        vectors = await provider.embed_texts(texts)
        query_vectors = await provider.embed_texts([q.query for q in questions])

        # 折叠输入：与生产共用 result_collapse，不在这里另写一套归组规则。
        # `_Chunk` 没有 table_part，用**整表的起始位置**当表号——与生产里
        # 「同一张表一个 table_part」同语义，也不会把两张表互相覆盖。
        _rows: dict[object, list[tuple[int, str, str]]] = {}
        _tables: dict[object, str] = {}
        hits_by_id: dict[str, object] = {}
        current_part: dict[str, int] = {}
        for index, cid in enumerate(ids):
            chunk = by_id[cid]
            doc_stem = chunk.doc_stem
            unit_id = result_collapse.unit_id_of(chunk.heading_path)
            if chunk.chunk_type == "table":
                current_part[doc_stem] = chunk.start
                if unit_id:
                    _tables[
                        result_collapse.UnitKey(
                            result_collapse.UNIT_TABLE, doc_stem, unit_id, str(chunk.start)
                        )
                    ] = chunk.text
            part = current_part.get(doc_stem, 0)
            hits_by_id[cid] = result_collapse.Hit(
                chunk_id=cid,
                document_id=doc_stem,
                score=0.0,
                content=chunk.text,
                chunk_type=chunk.chunk_type,
                section_id="",
                parent_section_id="",
                table_part=part,
                heading_path=chunk.heading_path,
            )
            if unit_id:
                unit = result_collapse.UnitKey(result_collapse.UNIT_SECTION, doc_stem, unit_id)
                _rows.setdefault(unit, []).append((index, chunk.text, cid))
        section_rows = {k: tuple(v) for k, v in _rows.items()}

        cases = []
        delivered_cases = []
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

            sliced = ranked[: K * OVERFETCH_FACTOR]
            units = result_collapse.collapse(
                [hits_by_id[cid] for cid in sliced],
                sections=section_rows,
                tables=_tables,
                top_k=K,
            )
            delivered_cases.append(
                Case(
                    id=question.id,
                    type=question.type,
                    relevant=frozenset(
                        {result_collapse.unit_key(hits_by_id[cid]) for cid in relevant}
                    ),
                    returned=tuple(unit.unit_key for unit in units),
                )
            )

        m = aggregate(cases, K)
        dm = aggregate(delivered_cases, K)
        n = sum(len(c.relevant) for c in cases) / len(cases)
        per_type_all[key] = per_type
        overall[key] = (m, dm)
        print(
            f"{label}\n"
            f"  片数={len(chunks)} 均长={sum(len(c.text) for c in chunks) / len(chunks):.0f}"
            f" 参评={len(cases)} 无相关={no_rel} N={n:.2f}\n"
            f"  chunk 级  R@5={float(m['recall_at_k']):.4f} R@1={float(m['recall_at_1']):.4f}"
            f" hit@1={float(m['hit_at_1']):.4f} MRR={float(m['mrr']):.4f}\n"
            f"  交付级  R@5={float(dm['recall_at_k']):.4f} R@1={float(dm['recall_at_1']):.4f}"
            f" hit@1={float(dm['hit_at_1']):.4f} MRR={float(dm['mrr']):.4f}"
        )

    a, b = per_type_all["old"], per_type_all["new"]
    print("\n按题型 hit@1（同判据、同 127 题）")
    print(f"{'题型':18s} {'题数':>4} {'03':>7} {'现在':>7} {'差值':>8}")
    for qtype in sorted(set(a) | set(b), key=lambda t: -(_r(b, t) - _r(a, t))):
        print(
            f"{qtype:18s} {len(a.get(qtype, [])):4d} {_r(a, qtype):7.3f}"
            f" {_r(b, qtype):7.3f} {_r(b, qtype) - _r(a, qtype):+8.3f}"
        )

    old_m, old_d = overall["old"]
    new_m, new_d = overall["new"]
    print("\n整体（同判据、同题数）：两个口径都要看")
    print(f"{'':10s} {'03 时代':>9} {'现在':>9} {'差值':>8}")
    for name, metric in (
        ("chunk 级 hit@1", "hit_at_1"),
        ("交付级 hit@1", "hit_at_1"),
        ("chunk 级 MRR", "mrr"),
        ("交付级 MRR", "mrr"),
    ):
        left = old_m if name.startswith("chunk") else old_d
        right = new_m if name.startswith("chunk") else new_d
        print(
            f"{name:16s} {float(left[metric]):9.4f} {float(right[metric]):9.4f}"
            f" {float(right[metric]) - float(left[metric]):+8.4f}"
        )


def _r(side: dict[str, list[bool]], qtype: str) -> float:
    flags = side.get(qtype) or []
    return sum(flags) / len(flags) if flags else 0.0


if __name__ == "__main__":
    asyncio.run(_run())
