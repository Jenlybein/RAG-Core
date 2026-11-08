"""切块策略自评：朴素字符切块 vs Markdown 结构化切块（用仓库自己的评测语料）。

为什么另写一个，而不是改 `eval_retrieval.py`：那支脚本比的是**检索路径**
（vector / bm25 / hybrid），它把索引与 ES、PG 都跑起来；本脚本只比**切块策略**，
所以不落库、不碰 ES，把同一份语料切两遍、同一套 embedding 排两遍序即可。

复用的东西（不另造）：

- 语料：`eval/corpus/*.md` + `manifest.json`（9 篇，约 3.7 万字，按 manifest 的库划分）；
- 题库：`eval/questions.json`（24 道，标注为 ``(文档, heading_path)``，含 exact_code /
  rare_token / model_number / colloquial / synonym / cross_section / absent 七种题型）；
- 算分：`scripts/eval_metrics.py` 的 ``Case`` / ``aggregate``，与第 4 步验收用的同一套口径。

两组都按**生产口径**拼检索文本（`build_retrieval_text(标题, 章节路径, 正文)`，见
GLOSSARY 的「检索文本」）：朴素切块没有章节路径，就只有「文档 + 正文」两段；
结构化切块多一段「章节」。这样差的只有切块策略本身，不是文本拼法。

判据（关键设计，也是与 `eval_retrieval.py` 唯一的差别）：

- 每道题的金标是**章节**，所以先算每个切片的「归属章节」：它与该章节字符区间的重叠
  必须占自身长度的 60% 以上，否则记为「跨章节」、不归属任何章节；
- 一个切片算命中，当且仅当它归属的章节 = 金标章节。
- 这样朴素切块的两个典型失效会如实掉分：切片跨章节（归属为 None）、章节被并进别的切片。

用法：`uv run python scripts/eval_chunking.py`（需要 `.env` 里的 Model 配置，会发真实请求）。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.rag_service import get_embedding_provider
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.retrieval_text import build_retrieval_text
from app.utils.text_splitter import CharacterTextSplitter

try:  # 与 scripts/eval_retrieval.py 的目录约定一致：脚本目录直接可导
    from eval_metrics import Case, aggregate
except ImportError:  # pragma: no cover - 兜底：直接 import 失败时按同口径本地实现
    Case = None  # type: ignore[assignment]
    aggregate = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
TABLE_MAX_ROWS = 10
DOMINANCE_RATIO = 0.6

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class CorpusDoc:
    stem: str
    title: str
    text: str


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    query: str
    labels: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Section:
    heading_path: str
    start: int
    end: int


@dataclass
class Chunk:
    text: str
    doc_stem: str
    heading_path: str
    start: int
    end: int
    owner: str | None = None  # 归属章节；无法归属（跨章节）时为 None


def _load_corpus() -> list[CorpusDoc]:
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    docs: list[CorpusDoc] = []
    for kb in manifest["kbs"]:
        for filename in kb["documents"]:
            path = CORPUS_DIR / filename
            text = path.read_text(encoding="utf-8")
            title = next(
                (line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem
            )
            docs.append(CorpusDoc(stem=path.stem, title=title, text=text))
    return docs


def _load_questions() -> tuple[list[Question], int]:
    payload = json.loads((EVAL_DIR / "questions.json").read_text(encoding="utf-8"))
    questions = [
        Question(
            id=item["id"],
            type=item.get("type", ""),
            query=item["query"],
            labels=tuple((label["document"], label["heading_path"]) for label in item["relevant"]),
        )
        for item in payload["questions"]
    ]
    return questions, int(payload.get("k", 5))


def _sections(text: str) -> list[Section]:
    """按标题切出章节区间，与 ``MarkdownStructuredSplitter`` 同语义。

    h1/h2 关闭同级或更深的旧章节（于是 h2 不会串到上一个 h2），h3 及以下只加深路径。
    章节路径是「祖先标题 > 本节标题」用 `` > `` 连接，和切块器写进 ``heading_path``
    的格式一致——这是本脚本能拿标注当判据的前提。
    """

    sections: list[Section] = []
    stack: list[tuple[int, str, int]] = []  # (level, title, start)

    def close(level: int, title: str, start: int, end: int) -> None:
        ancestors = " > ".join(t for _, t, _ in stack)
        path = f"{ancestors} > {title}" if ancestors else title
        sections.append(Section(heading_path=path, start=start, end=end))

    offset = 0
    for line in text.splitlines(keepends=True):
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            # 只关掉「同级或更深」的章节：h3 不该把外层的 h2 关掉。
            while stack and stack[-1][0] >= level:
                _, old_title, old_start = stack.pop()
                close(level, old_title, old_start, offset)
            stack.append((level, title, offset))
        offset += len(line)
    while stack:
        _, title, start = stack.pop()
        close(0, title, start, len(text))
    return sections


def _owner_of(chunk: Chunk, sections: list[Section]) -> str | None:
    """切片的归属章节：自身 >= 60% 的字符落在同一章节里才算，否则算跨章节。"""

    best: tuple[int, str] | None = None
    for section in sections:
        overlap = min(chunk.end, section.end) - max(chunk.start, section.start)
        if overlap <= 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, section.heading_path)
    if best is None or best[0] < DOMINANCE_RATIO * len(chunk.text):
        return None
    return best[1]


def _naive_chunks(doc: CorpusDoc, sections: list[Section]) -> list[Chunk]:
    splitter = CharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks: list[Chunk] = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    start = 0
    for piece in splitter.split(doc.text):
        end = start + len(piece.text)
        chunks.append(Chunk(piece.text, doc.stem, "", start, end))
        start += step
    for chunk in chunks:
        chunk.owner = _owner_of(chunk, sections)
    return chunks


def _structured_chunks(doc: CorpusDoc, sections: list[Section]) -> list[Chunk]:
    splitter = MarkdownStructuredSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, table_max_rows=TABLE_MAX_ROWS
    )
    chunks: list[Chunk] = []
    cursor = 0
    for piece in splitter.split(doc.text):
        position = doc.text.find(piece.text, cursor)
        if position < 0:
            position = doc.text.find(piece.text)
        if position < 0:
            position = cursor
        cursor = position + len(piece.text)
        heading_path = str(piece.metadata.get("heading_path") or "")
        chunks.append(Chunk(piece.text, doc.stem, heading_path, position, cursor))
    # 结构化切块的归属直接取切块器给的章节路径（它就是切块时的归属，不需要再猜）
    for chunk in chunks:
        chunk.owner = chunk.heading_path or None
    return chunks


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # Provider 输出已归一化


def _manual_metrics(cases: list[tuple[set[str], list[str]]], k: int) -> dict[str, float]:
    """与 eval_metrics 同口径的兜底实现：precision@k / recall@k / MRR / R@1 / hit@1。"""

    precision = recall = reciprocal = recall1 = hit1 = 0.0
    answerable = [(relevant, returned) for relevant, returned in cases if relevant]
    for relevant, returned in answerable:
        top = returned[:k]
        precision += len(set(top) & relevant) / k
        recall += len(set(top) & relevant) / len(relevant)
        first = next((rank for rank, cid in enumerate(returned, 1) if cid in relevant), None)
        reciprocal += 1.0 / first if first else 0.0
        recall1 += 1.0 if (top[:1] and top[0] in relevant) else 0.0
        hit1 += 1.0 if first == 1 else 0.0
    n = len(answerable) or 1
    return {
        "count": len(answerable),
        "precision_at_k": precision / n,
        "recall_at_k": recall / n,
        "mrr": reciprocal / n,
        "recall_at_1": recall1 / n,
        "hit_at_1": hit1 / n,
    }


async def main() -> None:
    docs = _load_corpus()
    questions, k = _load_questions()
    answerable = [q for q in questions if q.labels]
    print(
        f"语料：{len(docs)} 篇 / {sum(len(d.text) for d in docs)} 字；题目：{len(questions)} 道"
        f"（可回答 {len(answerable)} 道）；k = {k}"
    )

    provider = get_embedding_provider()
    section_cache = {doc.stem: _sections(doc.text) for doc in docs}

    summary: dict[str, dict[str, object]] = {}
    for variant in ("naive", "structured"):
        chunks: list[Chunk] = []
        for doc in docs:
            build = _naive_chunks if variant == "naive" else _structured_chunks
            chunks.extend(build(doc, section_cache[doc.stem]))

        chunk_ids = [f"{variant}-c{i:04d}" for i in range(len(chunks))]
        # 生产口径的检索文本：文档名 + 章节路径 + 正文（朴素切块没有章节段）
        retrieval_texts = [
            build_retrieval_text(
                next(d.title for d in docs if d.stem == chunk.doc_stem),
                chunk.heading_path,
                chunk.text,
            )
            for chunk in chunks
        ]
        vectors = await provider.embed_texts(retrieval_texts)
        query_vectors = await provider.embed_texts([q.query for q in answerable])

        section_map = {
            (doc.stem, section.heading_path): section
            for doc in docs
            for section in section_cache[doc.stem]
        }
        cases: list[tuple[set[str], list[str]]] = []
        unowned = sum(1 for chunk in chunks if chunk.owner is None)
        no_chunk_questions = 0
        for question, query_vector in zip(answerable, query_vectors):
            relevant: set[str] = set()
            for cid, chunk in zip(chunk_ids, chunks):
                for label in question.labels:
                    section = section_map.get(label)
                    if section is None or chunk.doc_stem != label[0]:
                        continue
                    overlap = min(chunk.end, section.end) - max(chunk.start, section.start)
                    if overlap <= 0:
                        continue
                    # 取「切片」与「章节」里较短的那个做分母：大切片吞下小节、
                    # 小节被长章节切出的小片，两种情况都算命中，两组对称。
                    if overlap >= DOMINANCE_RATIO * min(
                        len(chunk.text), section.end - section.start
                    ):
                        relevant.add(cid)
                        break
            if not relevant:
                no_chunk_questions += 1
            scores = sorted(
                ((_cosine(query_vector, vector), cid) for vector, cid in zip(vectors, chunk_ids)),
                reverse=True,
            )
            cases.append((relevant, [cid for _, cid in scores]))

        if Case is not None and aggregate is not None:
            metrics = aggregate(
                [
                    Case(id=q.id, type=q.type, relevant=frozenset(rel), returned=tuple(ret))
                    for q, (rel, ret) in zip(answerable, cases)
                ],
                k=k,
            )
        else:
            metrics = _manual_metrics(cases, k)

        summary[variant] = {
            "chunks": len(chunks),
            "avg_len": sum(len(c.text) for c in chunks) // len(chunks),
            "unowned": unowned,
            "no_chunk_questions": no_chunk_questions,
            **metrics,
        }

        print(f"\n=== {variant} ===")
        print(
            f"切片数 = {len(chunks)}，均长 = {sum(len(c.text) for c in chunks) // len(chunks)} 字"
        )
        print(f"跨章节切片（归属不到任何章节）= {unowned}/{len(chunks)}")
        print(f"语料里找不到对应切片的题目 = {no_chunk_questions}/{len(answerable)}")
        if variant == "structured":
            missing_labels = {
                label
                for question in answerable
                for label in question.labels
                if label not in {(c.doc_stem, c.owner) for c in chunks}
            }
            print(f"结构化切片里缺席的金标章节 = {len(missing_labels)} 个")
            for label in sorted(missing_labels)[:5]:
                print(f"  缺席：{label[0]} / {label[1]}")

        def fmt(value: object) -> str:
            return f"{float(value):.4f}" if value is not None else "n/a"

        print(f"P@{k} = {fmt(metrics['precision_at_k'])}")
        print(f"R@{k} = {fmt(metrics['recall_at_k'])}")
        print(f"MRR   = {fmt(metrics['mrr'])}")
        print(f"R@1   = {fmt(metrics['recall_at_1'])}")
        print(f"hit@1 = {fmt(metrics['hit_at_1'])}")

    print("\n=== 差值（结构化 - 朴素）===")
    for key in ("precision_at_k", "recall_at_k", "mrr", "recall_at_1", "hit_at_1"):
        left = summary["structured"][key]
        right = summary["naive"][key]
        if left is None or right is None:
            print(f"{key:14s} n/a")
            continue
        print(f"{key:14s} {float(left) - float(right):+.4f}")


if __name__ == "__main__":
    asyncio.run(main())
