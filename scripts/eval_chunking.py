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
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.rag_service import get_embedding_provider
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.tokenizer import token_count
from app.utils.window_splitter import LengthFunction, WindowMode
from app.utils.retrieval_text import build_retrieval_text

try:  # 与 scripts/eval_retrieval.py 的目录约定一致：脚本目录直接可导
    from eval_metrics import Case, aggregate
except ImportError:  # pragma: no cover - 兜底：直接 import 失败时按同口径本地实现
    Case = None  # type: ignore[assignment]
    aggregate = None  # type: ignore[assignment]

from app.services import result_collapse
from app.services.result_collapse import OVERFETCH_FACTOR

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"

# 窗口可被环境变量覆盖，用来做参数网格调优（见 docs 里的「参数调优」一节）。
# 不设变量时就是生产默认值——所以直接跑脚本得到的就是生产口径的数字。
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE") or 800)
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP") or 100)
TABLE_MAX_ROWS = 10
DOMINANCE_RATIO = 0.6

# 长章节对照组：这三篇刻意让每节 700-1100 字（超过 chunk_size）。
# 其余文档的章节中位只有 144 字，一个 800 字窗口能装 5-6 节。
# 分组报告用来回答「语料结构本身是不是把差距压小了」。
LONG_SECTION_DOCS = frozenset({"deploy_guide", "troubleshoot_deep", "integration_api"})

# `false_premise` 不进主平均：它的正确行为是「识别前提有误」，而纯向量检索
# 只会返回「最像问题本身」的切片，不会去找反驳材料。实测两组都是 0.000，
# 留着只会白占分母、把真实差距压小。单独统计，不进平均值。
EXCLUDED_FROM_MAIN = frozenset({"false_premise"})


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
    # 下面三项供「答案单元」归并使用：行级表片要能映射回它所属的整表片。
    chunk_type: str = ""
    section_id: str = ""
    parent_section_id: str = ""
    table_part: int = 0


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
    """列出每个标题的区间与完整路径（h1..h6，与切块器的 heading_path 同语义）。

    为什么每个层级都要列，而不只列 h1/h2：金标已经钉到子节级（``A > B > C``），
    一条金标必须能在表里查到它自己的区间，否则「朴素切块」那两组的命中会被算成
    零——不是切法变差，是判据找不到对应章节。

    区间终点取「下一个同级或更浅的标题」：h3 的范围到下一个 h1/h2/h3 为止，
    h4 的范围到下一个 h1..h4 为止。这才符合「一节包含自己的子节」的直觉。
    """

    sections: list[Section] = []
    stack: list[tuple[int, str, int]] = []  # (level, title, start)

    def close(level: int, start: int, end: int) -> None:
        # 关掉栈里所有「同级或更深」的条目：它们的区间都停在 end。
        while stack and stack[-1][0] >= level:
            _, title, old_start = stack.pop()
            ancestors = " > ".join(t for _, t, _ in stack)
            path = f"{ancestors} > {title}" if ancestors else title
            sections.append(Section(heading_path=path, start=old_start, end=end))

    offset = 0
    for line in text.splitlines(keepends=True):
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            close(level, offset, offset)
            stack.append((level, title, offset))
        offset += len(line)
    close(1, 0, len(text))
    while stack:
        _, title, start = stack.pop()
        ancestors = " > ".join(t for _, t, _ in stack)
        path = f"{ancestors} > {title}" if ancestors else title
        sections.append(Section(heading_path=path, start=start, end=len(text)))
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


# 最终形态（生产默认）：Recursive 分区 + Qwen token 计长。
# 报告的「按题型 / 按分组 / 逐题」都围绕它展开，其余三组只出现在对照表里。
FINAL_VARIANT = "D-rec-token"


def _fixed_window_slice(text: str, *, size: int, overlap: int) -> list[str]:
    """朴素基线：纯字符窗口硬切，不认识任何结构。

    这是**评测专用**的实现，刻意不从生产 import：生产正在换成按段落/句子的切法，
    而对照必须留一个「什么都不懂」的参照物。两件事各自独立，改一边不会牵连另一边。

    行为与历史上那版生产实现逐字相同（步长 = size - overlap，不回溯、不 strip），
    所以 naive-400/100 与 naive-200/50 的历史数字仍可比。
    """

    if not text:
        return []
    step = size - overlap
    pieces: list[str] = []
    start = 0
    while start < len(text):
        pieces.append(text[start : start + size])
        start += step
    return pieces


def _naive_chunks(
    doc: CorpusDoc, sections: list[Section], *, size: int, overlap: int
) -> list[Chunk]:
    chunks: list[Chunk] = []
    step = size - overlap
    start = 0
    for text in _fixed_window_slice(doc.text, size=size, overlap=overlap):
        end = start + len(text)
        chunks.append(Chunk(text, doc.stem, "", start, end))
        start += step
    for chunk in chunks:
        chunk.owner = _owner_of(chunk, sections)
    return chunks


def _structured_chunks(
    doc: CorpusDoc,
    sections: list[Section],
    *,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    length_function: LengthFunction = token_count,
    window_mode: WindowMode = "recursive",
) -> list[Chunk]:
    splitter = MarkdownStructuredSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        table_max_rows=TABLE_MAX_ROWS,
        length_function=length_function,
        window_mode=window_mode,
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
        chunks.append(
            Chunk(
                piece.text,
                doc.stem,
                heading_path,
                position,
                cursor,
                chunk_type=str(piece.metadata.get("chunk_type") or ""),
                section_id=str(piece.metadata.get("section_id") or ""),
                parent_section_id=str(piece.metadata.get("parent_section_id") or ""),
                table_part=int(piece.metadata.get("table_part") or 0),
            )
        )
    # 结构化切块的归属直接取切块器给的章节路径（它就是切块时的归属，不需要再猜）
    for chunk in chunks:
        chunk.owner = chunk.heading_path or None
    return chunks


def _answer_unit_of(chunk: Chunk, chunk_id: str) -> tuple[str, ...]:
    """把切片映射成**答案单元**——注意与 `result_collapse` 的「交付单元」不是一回事。

    答案单元用来算**chunk 级**指标：一片能不能答这道题。行级表片归到它所属的整表，
    免得一张 8 行的表把 R@1 的分母撑成 9。
    交付单元是折叠之后的返回粒度，由生产同一个 `result_collapse.collapse` 算出来。
    两者名字必须能区分，否则「到底是哪个口径」又会含混。

    行级表片归到它所属的整表片：用户问的是表里某个单元格，答案单元是那张表。
    这样行级片命中仍算「命中了答案」，而分母只有 1——两侧口径一致，指标才可解释。

    其余片各自成一个单元（用 chunk_id 作为身份）。
    """

    if chunk.chunk_type == "table_row" and chunk.section_id:
        return ("table", chunk.doc_stem, chunk.section_id, str(chunk.table_part))
    return ("chunk", chunk_id)


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
    excluded = [q for q in questions if q.type in EXCLUDED_FROM_MAIN]
    answerable = [q for q in questions if q.labels and q.type not in EXCLUDED_FROM_MAIN]
    print(
        f"语料：{len(docs)} 篇 / {sum(len(d.text) for d in docs)} 字；题目：{len(questions)} 道"
        f"（可回答 {len(answerable)} 道）；k = {k}"
    )
    if excluded:
        print(
            f"不进主平均：{len(excluded)} 道 {sorted({q.type for q in excluded})} "
            f"——纯向量检索无法处理「前提有误」，两组实测同为 0"
        )

    provider = get_embedding_provider()
    section_cache = {doc.stem: _sections(doc.text) for doc in docs}

    summary: dict[str, dict[str, object]] = {}
    # 金标章节在结构化切块下的类型（table / prose / code）：给「表格专项」分组用。
    # 只算一次，不进 variant 循环——它是语料属性，与切块粒度无关。
    gold_types: dict[tuple[str, str], str] = {}
    _probe = MarkdownStructuredSplitter(CHUNK_SIZE, CHUNK_OVERLAP, TABLE_MAX_ROWS)
    # 同一节可能产出多种片（整表片 + 行级片、prose + code），后来的片会覆盖前一片的
    # 类型。按优先级取「最能代表这节形态」的那一种：有表就是表格节，其次代码、
    # 其次行级表、再次列表、最后才是普通正文。
    _priority = {"table": 4, "code": 3, "table_row": 2, "list": 1, "prose": 0}
    for doc in docs:
        for piece in _probe.split(doc.text):
            heading = str(piece.metadata.get("heading_path") or "")
            chunk_type = str(piece.metadata.get("chunk_type") or "")
            key = (doc.stem, heading)
            if _priority.get(chunk_type, 0) >= _priority.get(gold_types.get(key, "prose"), 0):
                gold_types[key] = chunk_type
    # (名称, 切块器, chunk_size, overlap, 尺子, 窗口算法)
    #
    # 四组**交叉**是这一轮的核心对照：单位与算法同时改了，只跑前后两组说不清
    # 收益来自哪边。②−① 单独隔离单位、③−① 单独隔离算法、④−① 是合并效果。
    #
    # 朴素组保留纯字符硬切（400/100 与 200/50）：它们是历史基线，评测侧自带实现，
    # 与生产无关。
    variants: tuple[
        tuple[str, str, int | None, int | None, LengthFunction | None, WindowMode | None],
        ...,
    ] = (
        ("A-hard-char", "structured", 800, 100, len, "hard"),
        ("B-hard-token", "structured", 512, 50, token_count, "hard"),
        ("C-rec-char", "structured", 800, 100, len, "recursive"),
        ("D-rec-token", "structured", 512, 50, token_count, "recursive"),
        ("naive-400/100", "naive", 400, 100, None, None),
        ("naive-200/50", "naive", 200, 50, None, None),
    )
    for label, kind, size, overlap, length_function, window_mode in variants:
        chunks: list[Chunk] = []
        for doc in docs:
            if kind == "naive":
                assert size is not None and overlap is not None
                chunks.extend(
                    _naive_chunks(doc, section_cache[doc.stem], size=size, overlap=overlap)
                )
            else:
                assert size is not None and overlap is not None
                assert length_function is not None and window_mode is not None
                chunks.extend(
                    _structured_chunks(
                        doc,
                        section_cache[doc.stem],
                        size=size,
                        overlap=overlap,
                        length_function=length_function,
                        window_mode=window_mode,
                    )
                )

        tag = label.replace("/", "-")
        chunk_ids = [f"{tag}-c{i:04d}" for i in range(len(chunks))]
        # 生产口径的检索文本：文档名 + 章节路径 + 正文（朴素切块没有章节段）。
        # 设 SECTION_CONTEXT_PROXY=1 时再补一句「节说明」，用本节首句当代理——
        # 真说明由大模型生成，离线跑不出来；首句同样是「一句人话的节级上下文」，
        # 足以验证拼装生效与指标方向，但不能当成模型效果。
        # 只加在结构化组：朴素组必须保持为对照基线，否则差值不干净。
        contextual = os.environ.get("SECTION_CONTEXT_PROXY") == "1" and kind == "structured"
        section_first_sentence: dict[tuple[str, str], str] = {}
        if contextual:
            for chunk in chunks:
                key = (chunk.doc_stem, chunk.heading_path)
                if key in section_first_sentence:
                    continue
                head, _sep, _tail = chunk.text.partition("。")
                section_first_sentence[key] = head.strip() + "。"
        retrieval_texts = [
            build_retrieval_text(
                next(d.title for d in docs if d.stem == chunk.doc_stem),
                chunk.heading_path,
                chunk.text,
                section_first_sentence.get((chunk.doc_stem, chunk.heading_path))
                if contextual
                else None,
                # 默认开着，与生产一致（indexing_service 永远传 True）。
                # 想量「没有型号段」的旧口径时显式传 MODEL_TAG_PROXY=0。
                model_tags=os.environ.get("MODEL_TAG_PROXY", "1") == "1" and kind == "structured",
                # 与生产一致地传类型：行级表片不加型号段的规则在 build_retrieval_text 里，
                # 不传类型就等于把那条规则绕过去了，量出来的数字也不算数。
                chunk_type=chunk.chunk_type,
            )
            for chunk in chunks
        ]
        # 折叠输入：与生产同一套纯函数（result_collapse）。
        # 这里只准备数据，不算分——算分口径由同一份实现保证，不再手工复刻。
        hits_by_id = {
            cid: result_collapse.Hit(
                chunk_id=cid,
                document_id=chunk.doc_stem,
                score=0.0,
                content=chunk.text,
                chunk_type=chunk.chunk_type,
                section_id=chunk.section_id,
                parent_section_id=chunk.parent_section_id,
                table_part=chunk.table_part,
                heading_path=chunk.heading_path,
            )
            for cid, chunk in zip(chunk_ids, chunks)
        }
        _rows: dict[tuple[str, ...], list[tuple[int, str, str]]] = {}
        _tables: dict[tuple[str, ...], str] = {}
        for index, (cid, chunk) in enumerate(zip(chunk_ids, chunks)):
            unit_id = result_collapse.unit_id_of(chunk.heading_path)
            if unit_id:
                key = result_collapse.UnitKey(result_collapse.UNIT_SECTION, chunk.doc_stem, unit_id)
                _rows.setdefault(key, []).append((index, chunk.text, cid))
            if chunk.chunk_type == "table":
                _tables[
                    result_collapse.UnitKey(
                        result_collapse.UNIT_TABLE, chunk.doc_stem, unit_id, str(chunk.table_part)
                    )
                ] = chunk.text
        section_rows = {key: tuple(rows) for key, rows in _rows.items()}

        vectors = await provider.embed_texts(retrieval_texts)
        query_vectors = await provider.embed_texts([q.query for q in answerable])

        section_map = {
            (doc.stem, section.heading_path): section
            for doc in docs
            for section in section_cache[doc.stem]
        }
        cases: list[tuple[set[str], list[str]]] = []
        delivered_cases: list[tuple[set[tuple[str, ...]], list[tuple[str, ...]]]] = []
        delivered_hits: list[bool] = []
        delivered_per_type: dict[str, list[bool]] = {}
        case_types: list[str] = []
        case_groups: list[str] = []
        case_kinds: list[str] = []
        unowned = sum(1 for chunk in chunks if chunk.owner is None)
        no_chunk_questions = 0
        for question, query_vector in zip(answerable, query_vectors):
            relevant: set[str] = set()
            if kind == "structured":
                # 结构化切块的归属就是它写进 heading_path 的路径，精确相等即命中。
                # 不走区间重叠：一条金标可能命中同一节的多片（整表片 + 行级片），
                # 而它们都该算相关，这正是「片变碎」要如实反映出来的地方。
                gold_keys = {(gold[0], gold[1]) for gold in question.labels}
                for cid, chunk in zip(chunk_ids, chunks):
                    if (chunk.doc_stem, chunk.heading_path) in gold_keys:
                        relevant.add(cid)
            else:
                for cid, chunk in zip(chunk_ids, chunks):
                    for gold in question.labels:
                        section = section_map.get(gold)
                        if section is None or chunk.doc_stem != gold[0]:
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
            # 换算成「答案单元」再评测：行级表片归到它所属的整表片（见 _answer_unit_of）。
            # 相关单元与命中单元同一口径，行级片命中仍算对，但分母不再被行数撑大。
            by_id = dict(zip(chunk_ids, chunks))
            relevant_units = {_answer_unit_of(by_id[cid], cid) for cid in relevant}
            returned_units: list[tuple[str, ...]] = []
            seen_units: set[tuple[str, ...]] = set()
            for _score, cid in scores:
                unit = _answer_unit_of(by_id[cid], cid)
                if unit in seen_units:
                    continue
                seen_units.add(unit)
                returned_units.append(unit)
            cases.append((relevant_units, returned_units))

            # 交付口径：先按生产同样的 over-fetch 取候选，再折叠，再看首位。
            sliced = [cid for _score, cid in scores[: k * OVERFETCH_FACTOR]]
            units = result_collapse.collapse(
                [hits_by_id[cid] for cid in sliced],
                sections=section_rows,
                tables=_tables,
                top_k=k,
            )
            delivered_order = [unit.unit_key for unit in units]
            delivered_units = {result_collapse.unit_key(hits_by_id[cid]) for cid in relevant}
            delivered_cases.append((delivered_units, delivered_order))
            delivered_first_hit = bool(
                delivered_units and delivered_order and delivered_order[0] in delivered_units
            )
            delivered_hits.append(delivered_first_hit)
            delivered_per_type.setdefault(question.type, []).append(delivered_first_hit)
            case_types.append(question.type)
            case_groups.append(
                "long" if any(gold[0] in LONG_SECTION_DOCS for gold in question.labels) else "short"
            )
            # 表格专项：金标章节在结构化切块下被判定为 table，就归到「表格题」。
            # 这批题的答案依赖表头与行列对齐，是切块策略最该体现差异的地方。
            case_kinds.append(
                "table"
                if any(gold_types.get(gold) == "table" for gold in question.labels)
                else "prose"
            )

        # 按题型拆开看首位命中：整体差值会被题型分布稀释，
        # 拆开才能看清「结构化切块到底在哪类题上起作用」。
        per_type: dict[str, list[bool]] = {}
        # 再按「金标章节是长是短」分组：长章节库每节 700-1100 字，一个 800 字窗口
        # 装不下；短章节库每节中位 144 字，窗口能装 5-6 节。这组对照用来回答
        # 「语料结构本身是不是把差距压小了」。
        per_group: dict[str, list[bool]] = {}
        per_kind: dict[str, list[tuple[bool, int]]] = {}
        for qtype, group, kind_, (rel, ret) in zip(case_types, case_groups, case_kinds, cases):
            if not rel:
                continue
            hit = bool(ret[:1] and ret[0] in rel)
            per_type.setdefault(qtype, []).append(hit)
            per_group.setdefault(group, []).append(hit)
            # 存 (是否首位命中, 相关片数) 两项：报告要同时给 hit@1 与平均相关片数，
            # 不然「表格节被拆成 9 片」这种事实会伪装成「检索变差」。
            per_kind.setdefault(kind_, []).append((hit, len(rel)))

        # R@1 的分母陷阱：一道题有 N 个相关切片时，单片最多覆盖 1/N，
        # 于是 R@1 的理论上限就是 1/N。切片越碎、N 越大，R@1 天然越低。
        # 不把这条单独打出来，就会把「答案被切成好几片」误读成「检索变差了」。
        relevant_sizes = [len(rel) for rel, _ in cases if rel]
        avg_relevant = sum(relevant_sizes) / len(relevant_sizes) if relevant_sizes else 0.0
        ceiling = (
            sum(1.0 / size for size in relevant_sizes) / len(relevant_sizes)
            if relevant_sizes
            else 0.0
        )
        top1_correct = sum(1.0 for rel, ret in cases if rel and ret[:1] and ret[0] in rel) / (
            len([1 for rel, _ in cases if rel]) or 1
        )

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

        summary[label] = {
            "chunks": len(chunks),
            "avg_len": sum(len(c.text) for c in chunks) // len(chunks),
            "unowned": unowned,
            "no_chunk_questions": no_chunk_questions,
            "avg_relevant": avg_relevant,
            "per_type": per_type,
            "per_group": per_group,
            "per_kind": per_kind,
            "ceiling": ceiling,
            "delivered": (_manual_metrics(delivered_cases, k) if delivered_cases else {}),
            "delivered_per_type": delivered_per_type,
            "delivered_hit_at_1": (
                sum(delivered_hits) / len(delivered_hits) if delivered_hits else 0.0
            ),
            "reach": (float(metrics["recall_at_1"]) / ceiling * 100) if ceiling else 0.0,
            **metrics,
        }

        print(f"\n=== {label} ===")
        print(
            f"切片数 = {len(chunks)}，均长 = {sum(len(c.text) for c in chunks) // len(chunks)} 字"
        )
        print(f"跨章节切片（归属不到任何章节）= {unowned}/{len(chunks)}")
        print(f"语料里找不到对应切片的题目 = {no_chunk_questions}/{len(answerable)}")
        print(f"平均每题相关切片数 = {avg_relevant:.2f} 片（R@1 理论上限 1/N = {ceiling:.3f}）")
        print(
            f"R@1 相对理论上限的达成率 = {(float(metrics['recall_at_1']) / ceiling * 100) if ceiling else 0.0:.1f}%"
        )
        print(f"首位落在相关切片内的比例 = {top1_correct * 100:.1f}%")
        if kind == "structured":
            missing_labels = {
                label_
                for question in answerable
                for label_ in question.labels
                if label_ not in {(c.doc_stem, c.owner) for c in chunks}
            }
            print(f"结构化切片里缺席的金标章节 = {len(missing_labels)} 个")
            for item in sorted(missing_labels)[:5]:
                print(f"  缺席：{item[0]} / {item[1]}")

        def fmt(value: object) -> str:
            return f"{float(value):.4f}" if value is not None else "n/a"

        print(f"P@{k} = {fmt(metrics['precision_at_k'])}")
        print(f"R@{k} = {fmt(metrics['recall_at_k'])}")
        print(f"MRR   = {fmt(metrics['mrr'])}")
        print(f"R@1   = {fmt(metrics['recall_at_1'])}")
        print(f"hit@1 = {fmt(metrics['hit_at_1'])}")

    keys = (
        "chunks",
        "avg_len",
        "avg_relevant",
        "ceiling",
        "reach",
        "recall_at_k",
        "recall_at_1",
        "hit_at_1",
        "mrr",
        "precision_at_k",
    )
    headers = (
        "切片数",
        "均长",
        "每题相关切片",
        "R@1上限",
        "达成率%",
        "R@5",
        "R@1",
        "hit@1",
        "MRR",
        "P@5",
    )
    print("\n=== 汇总 ===")
    print(" | ".join(f"{h:>12s}" for h in headers) + " |")
    for label, *_ in variants:
        row = summary[label]
        cells: list[str] = []
        for key in keys:
            value = row[key]
            if isinstance(value, float):
                cells.append(f"{value:>12.4f}")
            elif isinstance(value, int):
                cells.append(f"{value:>12d}")
            else:
                cells.append(f"{value!s:>12s}")
        print(" | ".join(cells) + f" | {label}")

    # 整体差值会被题型分布稀释：某一类题占比高、差距又小，就会把总和压平。
    # 拆开看才知道「结构化切块到底在哪类题上起作用」。
    naive_types = summary["naive-400/100"].get("per_type")
    struct_types = summary[FINAL_VARIANT].get("per_type")
    if isinstance(naive_types, dict) and isinstance(struct_types, dict):
        print("\n=== 按题型看 hit@1 ===")
        print("  题型               题数     朴素  结构化     差值")
        rows: list[tuple[str, int, float, float, float]] = []
        for qtype, flags_naive in naive_types.items():
            flags_struct = struct_types.get(qtype) or []
            if not flags_naive or not flags_struct:
                continue
            left = sum(flags_naive) / len(flags_naive)
            right = sum(flags_struct) / len(flags_struct)
            rows.append((qtype, len(flags_naive), left, right, right - left))
        for qtype, count, left, right, delta in sorted(rows, key=lambda r: -r[4]):
            print(f"  {qtype:16s} {count:6d} {left:8.3f} {right:7.3f} {delta:+8.3f}")

    # 短章节 vs 长章节：同一批代码、同一个模型，只换语料结构。
    naive_groups = summary["naive-400/100"].get("per_group")
    struct_groups = summary[FINAL_VARIANT].get("per_group")
    if isinstance(naive_groups, dict) and isinstance(struct_groups, dict):
        print("\n=== 按语料结构看 hit@1（短章节 vs 长章节）===")
        print("  语料结构           题数     朴素  结构化     差值")
        labels = {"short": "短章节（中位 144 字）", "long": "长章节（中位 974 字）"}
        for group in ("short", "long"):
            flags_naive = naive_groups.get(group) or []
            flags_struct = struct_groups.get(group) or []
            if not flags_naive or not flags_struct:
                continue
            left = sum(flags_naive) / len(flags_naive)
            right = sum(flags_struct) / len(flags_struct)
            print(
                f"  {labels[group]:22s} {len(flags_naive):5d} {left:8.3f} {right:7.3f} {right - left:+8.3f}"
            )

    # 表格专项：金标章节本身是表格的题，答案依赖表头与行列对齐。
    naive_kinds = summary["naive-400/100"].get("per_kind")
    struct_kinds = summary[FINAL_VARIANT].get("per_kind")
    if isinstance(naive_kinds, dict) and isinstance(struct_kinds, dict):
        print("\n=== 表格专项 vs 正文题（按金标章节的切片类型分组）===")
        print("  金标类型               题数    朴素  结构化     差值   相关片(朴)  相关片(结构)")
        for kind in ("table", "prose"):
            items_naive = naive_kinds.get(kind) or []
            items_struct = struct_kinds.get(kind) or []
            if not items_naive or not items_struct:
                continue
            left = sum(1 for hit, _n in items_naive if hit) / len(items_naive)
            right = sum(1 for hit, _n in items_struct if hit) / len(items_struct)
            avg_naive = sum(n for _hit, n in items_naive) / len(items_naive)
            avg_struct = sum(n for _hit, n in items_struct) / len(items_struct)
            name = "表格（答案在表里）" if kind == "table" else "正文（答案在段落里）"
            print(
                f"  {name:20s} {len(items_struct):5d} {left:8.3f} {right:8.3f}"
                f" {right - left:+8.3f}   {avg_naive:8.2f} {avg_struct:9.2f}"
            )

    print("\n=== 两口径对照（chunk 级 vs delivered 级）===")
    print(
        f"{'变体':22s} {'hit@1(chunk)':>13} {'hit@1(交付)':>13} {'R@5(交付)':>11} {'R@1(交付)':>11}"
    )
    for label, *_ in variants:
        row = summary[label]
        delivered = row.get("delivered") or {}
        print(
            f"{label:22s} {float(row['hit_at_1']):13.4f}"
            f" {float(row['delivered_hit_at_1']):13.4f}"
            f" {float(delivered.get('recall_at_k', 0.0)):11.4f}"
            f" {float(delivered.get('recall_at_1', 0.0)):11.4f}"
        )
    print(
        "  delivered 级 = 折叠之后实际交付给调用方的内容；chunk 级 = 折叠之前的小片。"
        "\n  两个口径都要看：只报对一方有利的那个，等于换靶子。"
    )

    # 交付口径按题型拆开：整体涨了不代表每类都涨，退步的那一类必须露出来。
    if FINAL_VARIANT in summary:
        chunk_types = summary[FINAL_VARIANT].get("per_type") or {}
        delivered_types = summary[FINAL_VARIANT].get("delivered_per_type") or {}
        if chunk_types and delivered_types:
            print(f"\n=== 按题型：chunk 级 → delivered 级（{FINAL_VARIANT}）===")
            print(f"  {'题型':18s} {'题数':>4} {'chunk 级':>9} {'交付级':>9} {'差值':>8}")
            rows = []
            for qtype, delivered_flags in delivered_types.items():
                if not delivered_flags:
                    continue
                chunk_flags = chunk_types.get(qtype) or []
                chunk_rate = sum(chunk_flags) / len(chunk_flags) if chunk_flags else 0.0
                delivered_rate = sum(delivered_flags) / len(delivered_flags)
                rows.append((qtype, len(delivered_flags), chunk_rate, delivered_rate))
            for qtype, count, chunk_rate, delivered_rate in sorted(
                rows, key=lambda r: -(r[3] - r[2])
            ):
                print(
                    f"  {qtype:18s} {count:4d} {chunk_rate:9.3f} {delivered_rate:9.3f}"
                    f" {delivered_rate - chunk_rate:+8.3f}"
                )

    print(f"\n=== 差值（{FINAL_VARIANT} - naive-400/100）===")
    for key in ("precision_at_k", "recall_at_k", "mrr", "recall_at_1", "hit_at_1"):
        left = summary[FINAL_VARIANT][key]
        right = summary["naive-400/100"][key]
        if left is None or right is None:
            print(f"{key:14s} n/a")
            continue
        print(f"{key:14s} {float(left) - float(right):+.4f}")

    print(f"\n=== 差值（{FINAL_VARIANT} - naive-200/50）===")
    for key in ("precision_at_k", "recall_at_k", "mrr", "recall_at_1", "hit_at_1"):
        left = summary[FINAL_VARIANT][key]
        right = summary["naive-200/50"][key]
        if left is None or right is None:
            print(f"{key:14s} n/a")
            continue
        print(f"{key:14s} {float(left) - float(right):+.4f}")


if __name__ == "__main__":
    asyncio.run(main())
