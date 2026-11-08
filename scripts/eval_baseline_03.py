"""用 03 那一章的配置跑一遍，和现在的配置对照：到底谁赢。

03 时代的切块器配置（还原）：
- 只有 h1/h2 是切片边界（SECTION_LEVEL = 2），h3 及更深只加深路径；
- 表格整块一片，不产出行级片；
- 代码块整块一片，不按窗口递归切；
- 检索文本只有「文档 + 章节 + 正文」，没有节说明、没有型号段。

其余条件（向量模型、语料、题目、top_k、评测口径）全部相同，只换切块器。
算分用与主评测相同的 Case / aggregate。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "scripts")

from app.providers.embedding import get_embedding_provider
from app.utils import markdown_splitter as ms
from app.utils.retrieval_text import build_retrieval_text
from eval_chunking import EXCLUDED_FROM_MAIN, _cosine, _load_corpus, _load_questions, _unit_of
from eval_metrics import Case, aggregate

K = 5
INCLUDE_FALSE_PREMISE = False


class _Chunk:
    """脚本内用的轻量切片：只带评测要的字段。"""

    __slots__ = (
        "text",
        "doc_stem",
        "heading_path",
        "chunk_type",
        "section_id",
        "table_part",
        "start",
        "end",
    )

    def __init__(self, piece, doc_stem: str, start: int = 0, end: int = 0) -> None:
        self.text = piece.text
        self.doc_stem = doc_stem
        self.heading_path = str(piece.metadata.get("heading_path") or "")
        self.chunk_type = str(piece.metadata.get("chunk_type") or "")
        self.section_id = str(piece.metadata.get("section_id") or "")
        self.table_part = int(piece.metadata.get("table_part") or 0)
        self.start = start
        self.end = end


def _structured(doc, *, level: int, rows: bool, code_split: bool, originals) -> list[_Chunk]:
    ms._table_row_texts = originals[0]
    ms.MarkdownStructuredSplitter._split_code_lines = originals[1]
    ms.MarkdownStructuredSplitter.SECTION_LEVEL = level
    if not rows:
        ms._table_row_texts = lambda _group: []
    if not code_split:
        ms.MarkdownStructuredSplitter._split_code_lines = lambda self, lines: [
            "\n".join(lines).rstrip()
        ]
    splitter = ms.MarkdownStructuredSplitter(800, 100, 10)
    chunks = [_Chunk(piece, doc.stem) for piece in splitter.split(doc.text)]
    # 还原补丁，避免影响下一组
    ms._table_row_texts = originals[0]
    ms.MarkdownStructuredSplitter._split_code_lines = originals[1]
    ms.MarkdownStructuredSplitter.SECTION_LEVEL = originals[2]
    return chunks


async def _run() -> None:
    docs = _load_corpus()
    questions, _ = _load_questions()
    questions = [q for q in questions if q.labels]
    if not INCLUDE_FALSE_PREMISE:
        questions = [q for q in questions if q.type not in EXCLUDED_FROM_MAIN]
    provider = get_embedding_provider()

    originals = (
        ms._table_row_texts,
        ms.MarkdownStructuredSplitter._split_code_lines,
        ms.MarkdownStructuredSplitter.SECTION_LEVEL,
    )

    variants = {
        "03 时代（h1/h2，整表整码）": (dict(level=2, rows=False, code_split=False), False),
        "现在（h3+行级表+代码切+型号段）": (dict(level=3, rows=True, code_split=True), True),
    }

    summary: dict[str, dict[str, object]] = {}
    for label, (cfg, model_tags) in variants.items():
        chunks = [c for doc in docs for c in _structured(doc, originals=originals, **cfg)]
        ids = [str(i) for i in range(len(chunks))]
        texts = [
            build_retrieval_text(
                next(d.title for d in docs if d.stem == c.doc_stem),
                c.heading_path,
                c.text,
                model_tags=model_tags,
            )
            for c in chunks
        ]
        vectors = await provider.embed_texts(texts)
        query_vectors = await provider.embed_texts([q.query for q in questions])
        by_id = dict(zip(ids, chunks))
        cases = []
        per_type: dict[str, list[bool]] = {}
        for question, qv in zip(questions, query_vectors):
            relevant = {
                _unit_of(by_id[cid], cid)
                for cid in ids
                if any(
                    by_id[cid].doc_stem == g[0] and by_id[cid].heading_path == g[1]
                    for g in question.labels
                )
            }
            if not relevant:
                continue
            ranked = []
            seen: set = set()
            for _s, cid in sorted(
                ((_cosine(qv, vec), cid) for vec, cid in zip(vectors, ids)), reverse=True
            ):
                unit = _unit_of(by_id[cid], cid)
                if unit in seen:
                    continue
                seen.add(unit)
                ranked.append(unit)
            cases.append(
                Case(
                    id=question.id,
                    type=question.type,
                    relevant=frozenset(relevant),
                    returned=tuple(ranked),
                )
            )
            per_type.setdefault(question.type, []).append(bool(ranked[0] in relevant))
        metrics = aggregate(cases, K)
        summary[label] = {
            "chunks": len(chunks),
            "avg_len": sum(len(c.text) for c in chunks) / len(chunks),
            "N": sum(len(c.relevant) for c in cases) / len(cases),
            "metrics": metrics,
            "per_type": per_type,
        }

    print(
        f"{'变体':36s} {'片数':>4} {'均长':>4} {'N':>4} {'R@5':>7} {'R@1':>7} {'hit@1':>7} {'MRR':>7}"
    )
    for label, row in summary.items():
        m = row["metrics"]
        print(
            f"{label:36s} {row['chunks']:4d} {row['avg_len']:4.0f} {row['N']:4.2f} "
            f"{float(m['recall_at_k']):7.4f} {float(m['recall_at_1']):7.4f} "
            f"{float(m['hit_at_1']):7.4f} {float(m['mrr']):7.4f}"
        )

    a = summary["03 时代（h1/h2，整表整码）"]["per_type"]
    b = summary["现在（h3+行级表+代码切+型号段）"]["per_type"]
    print("\n按题型 hit@1")
    print(f"{'题型':18s} {'题数':>4} {'03':>7} {'现在':>7} {'差值':>8}")
    for qtype in sorted(set(a) | set(b), key=lambda t: -_delta(a, b, t)):
        left = _rate(a, qtype)
        right = _rate(b, qtype)
        print(
            f"{qtype:18s} {len(a.get(qtype, [])):4d} {left:7.3f} {right:7.3f} {right - left:+8.3f}"
        )


def _rate(side: dict[str, list[bool]], qtype: str) -> float:
    flags = side.get(qtype) or []
    return sum(flags) / len(flags) if flags else 0.0


def _delta(a: dict[str, list[bool]], b: dict[str, list[bool]], qtype: str) -> float:
    return _rate(b, qtype) - _rate(a, qtype)


if __name__ == "__main__":
    asyncio.run(_run())
