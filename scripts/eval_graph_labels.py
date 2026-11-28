"""#158 · 图谱引用边的离线测量：拿标注集量 precision / recall，按文档形态分档。

**它是「否决方案的证据」，不是可运行的现役脚本**：#138 的范围变更撤销了轻量图谱
（ADR-0027），抽取器与关系边表都已删除。这个文件留着，是因为它记录的**方法与读数**是那次
否决的依据之一；要重新跑起来，回到 tag ``step11-with-graph-and-orchestration``。
直接跑现在的它会打印那段说明并以 **0** 退出（不是崩）——见 :func:`_load_rejected_scheme`。

**为什么当初要有这一层**：``eval/corpus-refs`` 的 9/9 是自测出来的——语料与抽取器出自同一份设计
文档，它只证明「我们自己写的那三种写法被认出来了」。要回答「真实文档里的写法被认出来多少」，
必须有一份**不属于本轮自造语料**的标注集（``eval/graph-labels/``），以及一个只算数、不解释的
测量脚本。

这个模块**不碰数据库、不发网络、不调模型**：语料是磁盘上的 Markdown，抽取器是生产那份
:func:`app.services.section_graph.extract_section_edges`（已随图谱撤销，见 tag），切块器是
生产那份 :class:`app.utils.markdown_splitter.MarkdownStructuredSplitter`。

## 测量单位与口径

- **测量单位 = 引用边**：只数 ``relation_type == reference``。兄弟边与父节边不来自正文
  （它们从标题路径算），把它们算进来只会把 precision 抬到 0.99 而什么问题也没回答。
- **源文档 = 被完整标注的那几篇**。同一档里其余文档只进目录、充当**解析目标**——它们的出边
  不进分母，因为没人为它们标注（没标注的分母只会造出一个假的 precision）。
- **TP**：抽出的边与某条 ``explicit`` 标注的（源文档, 源节, 目标文档, 目标节）四元组完全相同。
- **FP**：抽出的边没有这样的标注。目标错、整篇链接落到元信息块、在 ``noise`` 语境里建边，
  都算 FP（「方向对但粒度错」也是错——它会以假引用的形式出现在回答里）。
- **FN**：``explicit`` 标注里没有被抽出来的边。
- ``ambiguous`` / ``implicit`` / ``noise`` 三类**都不是真边**：它们上面建出来的边计入 FP，
  其中 ``noise`` 那一类单独统计（票面的「误报专项」）。

一份标注若指向不了「唯一一节」（整篇链接、裸条号、附件、范围引用），按 ADR-0027 决策 3 的
口径它本身就是弃权：标 ``ambiguous``，不进 recall 的分母。``implicit`` 是连定位符都没有的
指代型（「见上周那份」）；``noise`` 是提到但不依赖（格式示例、「已废止」）。

## 判据不在这里，在 eval/graph-labels/README.md

这个模块只负责「按 README 写死的口径算」，不重新解释什么算引用。改口径要改 README 与标注集，
不是改这里的分母。
"""

from __future__ import annotations

import json
import re
import sys

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.utils.markdown_splitter import MarkdownStructuredSplitter

#: 这个脚本测量的是**已否决方案**（轻量图谱的引用边抽取，ADR-0027）。它被 #138 的范围
#: 变更撤销了，实现只活在 tag 里：撤销的理由不是「规则不准」（补了 R1/R2 之后上限
#: recall 0.974），而是价值不成立——边只覆盖作者写了「见 X 节」的显式指路，饱和语料上
#: 注入 0 条，而规则维护成本随文档写法线性增长。
REJECTED_SCHEME_TAG = "step11-with-graph-and-orchestration"

#: 缺依赖时的唯一说明。**先说是已否决方案**，再指 tag，最后写对理由。
REJECTED_SCHEME_NOTE = (
    f"该脚本测量的是已否决方案（轻量图谱的关系边抽取），代码在 tag `{REJECTED_SCHEME_TAG}`。\n"
    "撤销理由不是抽取规则不准（补了 R1/R2 之后可确定性定位 37/38、上限 recall 0.974），\n"
    "而是价值不成立：边只存在于「见 X 节」这类显式指路里，而文档之间大部分有用的关联是隐式的。\n"
    f"要复现这份测量：git switch --detach {REJECTED_SCHEME_TAG}，再跑 uv run python "
    "scripts/eval_graph_labels.py。"
)

#: 已否决方案的符号**占位**：模块级 import 会在图谱代码消失之后直接 ImportError，
#: 于是「跑一下看它怎么说」变成一条红，而不是一句解释（#138 硬要求 2）。
#: ``main()`` 开头经 :func:`_load_rejected_scheme` 填真值；填不进来就印上面那段并退出 0。
EdgeDraft: Any = None
EdgeScope: Any = None
SectionCatalog: Any = None
SectionRecord: Any = None
SectionText: Any = None
extract_reference_candidates: Any = None
extract_section_edges: Any = None
RELATION_REFERENCE = "reference"


def _load_rejected_scheme() -> None:
    """把已否决方案的实现取进来；取不到就**打印 tag 指针并以 0 退出**。

    退出码 0 是刻意的：这不是脚本坏了，而是它测量的东西被有意撤掉了。
    """

    global EdgeDraft, EdgeScope, SectionCatalog, SectionRecord, SectionText
    global extract_reference_candidates, extract_section_edges, RELATION_REFERENCE

    try:
        from app.models.section_edge import RELATION_REFERENCE as relation_reference
        from app.services.section_graph import (
            EdgeDraft as edge_draft,
            EdgeScope as edge_scope,
            SectionCatalog as section_catalog,
            SectionRecord as section_record,
            SectionText as section_text,
            extract_reference_candidates as reference_candidates,
            extract_section_edges as section_edges,
        )
    except Exception:  # noqa: BLE001 - 探针只回答「还在不在」，不解释为什么不在
        print(REJECTED_SCHEME_NOTE)
        raise SystemExit(0) from None

    EdgeDraft = edge_draft
    EdgeScope = edge_scope
    SectionCatalog = section_catalog
    SectionRecord = section_record
    SectionText = section_text
    extract_reference_candidates = reference_candidates
    extract_section_edges = section_edges
    RELATION_REFERENCE = relation_reference


#: 票面阈值：**两个条件同时成立**才允许「不引入 LLM」。
RECALL_FLOOR = 0.7
PRECISION_FLOOR = 0.95

#: 四类标签。判据见 README；这里只用来校验标注文件没写错字。
LABELS = ("explicit", "ambiguous", "implicit", "noise")

#: 票面要求的六类表达式形态。
FORMS = ("title_section", "same_doc", "markdown_link", "anchor", "chinese_ordinal", "other")

#: 引用边的四元组：(源文档, 源标题路径, 目标文档, 目标标题路径)。
EdgeKey = tuple[str, str, str, str]

#: 测量过程中撞见、但与本票结论无关的事实（写进报告第八节，避免只在对话里说过）。
INCIDENTAL_FINDINGS = (
    "`docs/adr/0015-token-window-and-vocab.md` 的正文里含 `<|endoftext|>` 这个 tiktoken 特殊 "
    "token 的**字面量**，生产切块器（`app/utils/tokenizer.py`）对它直接抛 `ValueError: "
    "disallowed special token`。也就是说：上传一篇正文里出现这个字面量的文档，索引会失败。"
    "这一篇因此被排除在本测量的目录之外（`labels.json` 的 `exclude`），另记一笔。"
    "**（2026-11-15 已修，#160：正文里的特殊 token 字面量现在按数据编码，见 ADR-0015 末节。"
    "这段说明保留，因为它记录了那次测量的边界。）**",
    "测量集里曾有两类「解析侧」的漏法：①`本文件第 1 节` 这种写法完全在规则内的引用，在标题写成"
    "「第 N 节 X」的政策文档上解析失败——`section_by_number` 只认「标题以数字开头」；②整篇链接"
    "（`[ADR-0025](0025-….md) 决策五`）落到目标文档的 **h1 那一节**（ADR 的 h1 节只有「状态 / 日期 /"
    " 相关」三行元信息，边建出来了、方向也对、粒度却错）。**这两类都由 #158 决策 8 修掉**："
    "①改由「标题开头编号」归一匹配（含「第 N 条」「Section N」惯例）；②链接后面的「决策五」被吃进"
    "链接候选，落到决策条目那一节。",
    "**测量口径的两处适配**（文档更新在标注之后引入的，不是改判据）：ADR-0027 重写「决策 8」后，"
    "`（ADR-0025 决策五）` 这条 evidence 在本文出现了两处、`翻案条件` 这个目标标题也有两节同名；"
    "测量脚本因此用标注文件自带的 `source_section` 收窄证据所在节、用 `target.path` 区分同名节。"
    "两处都只在「唯一性被文档更新打破」时才用，判据（哪条算 TP / FP）不变。",
)

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class LabelItem:
    """一条标注：某篇文档里的一处引用表达，以及它「应该」长成什么。"""

    id: str
    tier: str
    document: str
    evidence: str
    label: str
    form: str
    form_detail: str = ""
    source_section: str = ""
    source_section_path: str | None = None
    target_document: str | None = None
    target_section: str | None = None
    target_path: str | None = None
    note: str = ""


@dataclass(frozen=True)
class LabelSet:
    """``eval/graph-labels/labels.json`` 的内存形状。"""

    tiers: Mapping[str, Mapping[str, str]]
    corpora: Mapping[str, Mapping[str, Sequence[str]]]
    documents: Mapping[str, str]
    items: tuple[LabelItem, ...]
    exclude: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def of_tier(self, tier: str) -> tuple[LabelItem, ...]:
        return tuple(item for item in self.items if item.tier == tier)

    def source_documents(self, tier: str) -> tuple[str, ...]:
        return tuple(str(key) for key in self.corpora[tier]["source_documents"])

    def target_globs(self, tier: str) -> tuple[str, ...]:
        return tuple(str(pattern) for pattern in self.corpora[tier].get("target_globs", ()))


def load_labels(path: Path) -> LabelSet:
    """读标注文件；任何结构问题都当场抛错（测量集的静默降级比没有测量更糟）。"""

    raw = json.loads(path.read_text(encoding="utf-8"))
    documents = {
        key: str(value["path"]) if isinstance(value, dict) else str(value)
        for key, value in raw["documents"].items()
    }
    items: list[LabelItem] = []
    for row in raw["labels"]:
        if row["label"] not in LABELS:
            raise ValueError(f"{row['id']}: 未知标签 {row['label']!r}")
        if row["form"] not in FORMS:
            raise ValueError(f"{row['id']}: 未知形态 {row['form']!r}")
        target = row.get("target") or {}
        items.append(
            LabelItem(
                id=str(row["id"]),
                tier=str(row["tier"]),
                document=str(row["document"]),
                evidence=str(row["evidence"]),
                label=str(row["label"]),
                form=str(row["form"]),
                form_detail=str(row.get("form_detail") or ""),
                source_section=str(row.get("source_section") or ""),
                source_section_path=row.get("source_section_path"),
                target_document=target.get("document"),
                target_section=target.get("section"),
                target_path=target.get("path"),
                note=str(row.get("note") or ""),
            )
        )
    return LabelSet(
        tiers=raw["tiers"],
        corpora=raw["corpora"],
        documents=documents,
        items=tuple(items),
        exclude=raw.get("exclude", {}),
    )


# ───────────────────────────────────────────────────────── 语料装配


@dataclass(frozen=True)
class TierCorpus:
    """一档语料：目录（解析目标用）+ 每篇源文档的切片。"""

    tier: str
    scope: EdgeScope
    catalog: SectionCatalog
    sections: Mapping[str, tuple[SectionText, ...]]
    documents: Mapping[str, str] = field(default_factory=dict)


def document_title_of(text: str, path: Path) -> str:
    """文档标题 = 首个 ``#`` 标题的文字（没有就用文件名主干）。

    生产路径里标题来自 ``documents.title``（上传时落的），Markdown 上传件就是它的 h1。
    《标题》第 N 节 这条规则对的是**标题**而不是文件名，所以这里必须按 h1 取。
    """

    match = _H1_RE.search(text)
    if match:
        return match.group(1).strip()
    return path.stem


def _documents_for(labels: LabelSet, tier: str, root: Path) -> dict[str, Path]:
    """这一档的「文档键 → 路径」：源文档显式列出，目标文档由 glob 补齐。"""

    excluded = set(labels.exclude.get(tier, ()))
    resolved: dict[str, Path] = {}
    for key in labels.source_documents(tier):
        resolved[key] = root / labels.documents[key]
    for pattern in labels.target_globs(tier):
        for path in sorted(root.glob(pattern)):
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            key = next(
                (known for known, known_path in labels.documents.items() if known_path == relative),
                relative,
            )
            resolved.setdefault(key, path)
    return resolved


def build_corpus(labels: LabelSet, tier: str, root: Path) -> TierCorpus:
    """切块、造目录，形状与索引期一致（#152 的 ``test_section_graph_corpus_refs`` 同款）。"""

    scope = EdgeScope(
        tenant_id="tenant-eval",
        kb_id=str(labels.tiers[tier]["kb_id"]),
    )
    documents = _documents_for(labels, tier, root)
    records: list[SectionRecord] = []
    per_document: dict[str, tuple[SectionText, ...]] = {}
    for key, path in documents.items():
        text = path.read_text(encoding="utf-8")
        pieces = MarkdownStructuredSplitter(document_id=key).split(text)
        title = document_title_of(text, path)
        entries: list[SectionText] = []
        for index, piece in enumerate(pieces):
            heading = str(piece.metadata.get("heading_path") or "")
            entries.append(
                SectionText(
                    record=SectionRecord(
                        tenant_id=scope.tenant_id,
                        kb_id=scope.kb_id,
                        document_id=key,
                        document_title=title,
                        section_id=str(piece.metadata.get("section_id") or ""),
                        heading_path=heading,
                        order=index,
                        source_filename=path.name,
                    ),
                    text=piece.text,
                )
            )
        per_document[key] = tuple(entries)
        records.extend(entry.record for entry in entries)
    return TierCorpus(
        tier=tier,
        scope=scope,
        catalog=SectionCatalog.of(records, scope=scope),
        sections=per_document,
        documents={key: str(path) for key, path in documents.items()},
    )


# ───────────────────────────────────────────────────────── 标注 → 事实


def section_of_evidence(corpus: TierCorpus, item: LabelItem) -> SectionText:
    """标注点所在的**节**；证据必须只落在一个节里。

    一个节可能有几片（长节切片之间有重叠，同一句会同时出现在两片里），所以唯一性按
    ``heading_path`` 数，不按片数数——测量单位是节。

    **文档更新后同一句可能在两节里出现**（例：ADR-0027 的「决策 8」把一条旧例子又复述了
    一遍，标注集写在更新之前）。标注文件本来就写了 ``source_section``/``source_section_path``，
    所以这时按标注收窄；收窄后仍不是恰好一节就照旧当场报错——**不猜**。
    """

    hits = [entry for entry in corpus.sections[item.document] if item.evidence in entry.text]
    headings = {entry.record.heading_path for entry in hits}
    if len(headings) != 1:
        narrowed = hits
        if item.source_section_path is not None:
            narrowed = [
                entry for entry in hits if entry.record.heading_path == item.source_section_path
            ]
        elif item.source_section:
            narrowed = [
                entry for entry in hits if entry.record.heading_title == item.source_section
            ]
        narrowed_headings = {entry.record.heading_path for entry in narrowed}
        if len(narrowed_headings) == 1:
            return narrowed[0]
        where = ", ".join(repr(heading) for heading in sorted(headings)) or "（一处都没有）"
        raise ValueError(
            f"{item.id}: 证据 `{item.evidence}` 落在 {len(headings)} 个节里：{where}"
            "——标注要能在唯一一个节里定位，否则测量对象本身就不确定"
        )
    return hits[0]


def resolve_target(corpus: TierCorpus, item: LabelItem) -> SectionRecord:
    """标注里的目标节 → 目录里的那一行；对不上就是标注写错了，当场抛。"""

    if item.target_document is None or item.target_section is None:
        raise ValueError(f"{item.id}: explicit 标注必须有 target")
    # 一个节可以有多片（长节会被切），所以「对上几处」按**标题路径**去重数，不按片数。
    entries = corpus.sections.get(item.target_document, ())
    if item.target_path is not None:
        matched = {
            e.record.heading_path: e for e in entries if e.record.heading_path == item.target_path
        }
    else:
        matched = {
            e.record.heading_path: e
            for e in entries
            if e.record.heading_title == item.target_section
        }
    if len(matched) != 1:
        raise ValueError(
            f"{item.id}: 目标节 {item.target_path or item.target_section!r} 在 "
            f"{item.target_document} 里对上 {len(matched)} 处（要求恰好一处）"
        )
    return next(iter(matched.values())).record


def expected_edges(corpus: TierCorpus, items: Iterable[LabelItem]) -> dict[EdgeKey, str]:
    """``explicit`` 标注 → 应该建出来的边（值是该标注的 id，用于报告里回指）。"""

    expected: dict[EdgeKey, str] = {}
    for item in items:
        if item.label != "explicit":
            continue
        source = section_of_evidence(corpus, item).record
        target = resolve_target(corpus, item)
        expected[
            (
                source.document_id,
                source.heading_path,
                target.document_id,
                target.heading_path,
            )
        ] = item.id
    return expected


# ───────────────────────────────────────────────────────── 建边与比对


def produced_edges(corpus: TierCorpus, source_documents: Iterable[str]) -> tuple[EdgeDraft, ...]:
    """只跑**被标注的源文档**，只留引用边。"""

    edges: list[EdgeDraft] = []
    for document in source_documents:
        entries = corpus.sections[document]
        if not entries:
            continue
        for edge in extract_section_edges(
            scope=corpus.scope,
            document_id=document,
            document_title=entries[0].record.document_title,
            sections=entries,
            catalog=corpus.catalog,
        ):
            if edge.relation_type == RELATION_REFERENCE:
                edges.append(edge)
    return tuple(edges)


def edge_key(edge: EdgeDraft) -> EdgeKey:
    return (
        edge.source_document_id,
        str(edge.metadata.get("source_heading_path") or ""),
        edge.target_document_id,
        str(edge.metadata.get("target_heading_path") or ""),
    )


def reconcile(
    produced: Sequence[EdgeDraft],
    expected: Mapping[EdgeKey, str],
    items: Sequence[LabelItem],
    corpus: TierCorpus,
) -> "TierScore":
    """把「建出来的边」与「标注的真边」对上，并给每条误报归因。"""

    by_section: dict[tuple[str, str], list[LabelItem]] = {}
    for item in items:
        section = section_of_evidence(corpus, item).record.heading_path
        by_section.setdefault((item.document, section), []).append(item)

    produced_keys = {edge_key(edge) for edge in produced}
    true_positive = produced_keys & set(expected)
    false_positive = produced_keys - set(expected)
    attribution: dict[EdgeKey, tuple[str, str]] = {}
    for edge in produced:
        key = edge_key(edge)
        if key in true_positive:
            attribution[key] = ("explicit", expected[key])
            continue
        origin = _origin_of(edge, by_section.get((edge.source_document_id, key[1]), []))
        attribution[key] = origin
    return TierScore(
        tier=corpus.tier,
        tp=tuple(sorted(true_positive)),
        fp=tuple(sorted(false_positive)),
        fn=tuple(sorted(set(expected) - produced_keys)),
        expected_count=len(expected),
        produced_count=len(produced_keys),
        false_negative_ids={key: expected[key] for key in set(expected) - produced_keys},
        attribution={key: value for key, value in attribution.items() if key in false_positive},
        edges=tuple(produced),
    )


def _origin_of(edge: EdgeDraft, candidates: Sequence[LabelItem]) -> tuple[str, str]:
    """一条误报的来源：它落在哪个标注点的原文片段上（没有就是「未标注」）。

    先看**原文包含**（``expression`` 与标注的 ``evidence`` 互相包含），对不上再看这一节是不是
    只有一个非 ``explicit`` 的标注——那就不用猜了，就是它。
    """

    expression = str(edge.metadata.get("expression") or "")
    for item in candidates:
        if expression and (expression in item.evidence or item.evidence in expression):
            return (item.label, item.id)
    others = [item for item in candidates if item.label != "explicit"]
    if len(others) == 1:
        return (others[0].label, others[0].id)
    return ("未标注", "")


@dataclass(frozen=True)
class TierScore:
    """一档的比对结果。"""

    tier: str
    tp: Sequence[EdgeKey]
    fp: Sequence[EdgeKey]
    fn: Sequence[EdgeKey]
    expected_count: int
    produced_count: int
    false_negative_ids: Mapping[EdgeKey, str]
    attribution: Mapping[EdgeKey, tuple[str, str]]
    edges: Sequence[EdgeDraft] = field(default_factory=tuple)

    @property
    def precision(self) -> float | None:
        return len(self.tp) / self.produced_count if self.produced_count else None

    @property
    def recall(self) -> float | None:
        return len(self.tp) / self.expected_count if self.expected_count else None


def recognition(corpus: TierCorpus, item: LabelItem) -> bool:
    """抽取器的**候选**认出这一处表达了吗（不看目标解析，只看正则认没认出）。"""

    entry = section_of_evidence(corpus, item)
    offset = entry.text.find(item.evidence)
    end = offset + len(item.evidence)
    for candidate in extract_reference_candidates(entry.text):
        if candidate.start < end and candidate.end > offset:
            return True
    return False


@dataclass(frozen=True)
class FormRow:
    """按表达式形态的一行。

    ``matched`` 是「这条标注的边真的建出来了、且目标对」；``exact`` 再加一条「而且是因为
    抽取器认出了**这一处表达**」。两者不等时那一条就是「撞对」：边对上了，但认出来的不是它
    （同一个节里另一处表达建出了同一条边）。
    """

    form: str
    label: str
    items: int
    recognized: int
    matched: int
    exact: int


def form_rows(
    corpus: TierCorpus, items: Sequence[LabelItem], score: TierScore
) -> tuple[FormRow, ...]:
    """形态 × 标签类别的计数表（``explicit`` 之外的三类 ``matched`` 恒为 0：它们不该有边）。"""

    counts: dict[tuple[str, str], list[int]] = {}
    for item in items:
        bucket = counts.setdefault((item.form, item.label), [0, 0, 0, 0])
        bucket[0] += 1
        seen = recognition(corpus, item)
        matched = _is_true(item, corpus, score)
        bucket[1] += int(seen)
        bucket[2] += int(matched)
        bucket[3] += int(matched and seen)
    return tuple(
        FormRow(
            form=form,
            label=label,
            items=values[0],
            recognized=values[1],
            matched=values[2],
            exact=values[3],
        )
        for (form, label), values in sorted(counts.items())
    )


def lucky_hits(
    corpus: TierCorpus, items: Sequence[LabelItem], score: TierScore
) -> tuple[LabelItem, ...]:
    """边对上了、但抽取器没有认出这条表达的那几条（「撞对」）。"""

    return tuple(
        item
        for item in items
        if item.label == "explicit"
        and _is_true(item, corpus, score)
        and not recognition(corpus, item)
    )


def unmatched_explicit(
    corpus: TierCorpus, items: Sequence[LabelItem], score: TierScore
) -> tuple[tuple[LabelItem, str], ...]:
    """漏掉的 ``explicit``：``(标注, 漏在哪一侧)``。

    三档，分法就是「要补的是什么」：

    * ``识别侧``：候选器根本没认出这种写法 → 缺的是**表达规则**；
    * ``解析侧·解析不出``：认出来了，但目标落不下去（标题编号形态、目标不在目录）；
    * ``解析侧·目标落到了别处``：建出了边，但落到另一个节上（整篇链接落到 h1 元信息块）。
    """

    wrong_target_ids = {identifier for _, identifier in score.attribution.values() if identifier}
    rows: list[tuple[LabelItem, str]] = []
    for item in items:
        if item.label != "explicit" or _is_true(item, corpus, score):
            continue
        if not recognition(corpus, item):
            rows.append((item, "识别侧（候选器没认出来）"))
        elif item.id in wrong_target_ids:
            rows.append((item, "解析侧·目标落到了别处（建了边，但落错节）"))
        else:
            rows.append((item, "解析侧·解析不出目标（认出来了，落不下去）"))
    return tuple(rows)


def missing_total(
    labels: LabelSet, corpora: Mapping[str, TierCorpus], scores: Mapping[str, TierScore]
) -> int:
    """三档漏掉的 ``explicit`` 合计（标题里要写这个数，不能手写死）。"""

    return sum(
        len(unmatched_explicit(corpora[tier], labels.of_tier(tier), scores[tier]))
        for tier in corpora
    )


def noise_rows(
    corpus: TierCorpus, items: Sequence[LabelItem], score: TierScore
) -> tuple[tuple[LabelItem, bool, str], ...]:
    """``noise`` 逐条的命运：认出了吗、最后长成什么。

    三种结局：``误报``（计入 FP）、``与另一条标注重合（撞对）``（建了边但恰好是真边）、
    ``没建成边``（认出来了但目标解析失败，或根本没认出）。
    """

    false_positive_ids = {identifier for _, identifier in score.attribution.values()}
    true_positive_sources = {(key[0], key[1]) for key in score.tp}
    rows: list[tuple[LabelItem, bool, str]] = []
    for item in items:
        if item.label != "noise":
            continue
        seen = recognition(corpus, item)
        source = section_of_evidence(corpus, item).record
        if item.id in false_positive_ids:
            outcome = "误报"
        elif seen and (source.document_id, source.heading_path) in true_positive_sources:
            outcome = "与另一条标注重合（撞对）"
        else:
            outcome = "没建成边"
        rows.append((item, seen, outcome))
    return tuple(rows)


def _is_true(item: LabelItem, corpus: TierCorpus, score: TierScore) -> bool:
    if item.label != "explicit":
        return False
    source = section_of_evidence(corpus, item).record
    target = resolve_target(corpus, item)
    return (
        source.document_id,
        source.heading_path,
        target.document_id,
        target.heading_path,
    ) in set(score.tp)


# ───────────────────────────────────────────────────────── 决策


@dataclass(frozen=True)
class Decision:
    """票面阈值的判定结果。"""

    recall: float | None
    precision: float | None
    introduce_llm: bool

    @property
    def reason(self) -> str:
        recall = "—" if self.recall is None else f"{self.recall:.4f}"
        precision = "—" if self.precision is None else f"{self.precision:.4f}"
        if self.introduce_llm:
            return (
                f"recall {recall} < {RECALL_FLOOR} 或 precision {precision} < "
                f"{PRECISION_FLOOR}：不满足「不上 LLM」的两个条件 → 引入 LLM 当候选提出者"
            )
        return (
            f"recall {recall} ≥ {RECALL_FLOOR} 且 precision {precision} ≥ {PRECISION_FLOOR}"
            " → 不引入 LLM，只补便宜缺口"
        )


def decide(recall: float | None, precision: float | None) -> Decision:
    """票面阈值：**两**个条件同时成立才不上 LLM；任何一项不成立都上 LLM。"""

    ok = (
        recall is not None
        and precision is not None
        and recall >= RECALL_FLOOR
        and precision >= PRECISION_FLOOR
    )
    return Decision(recall=recall, precision=precision, introduce_llm=not ok)


def pooled(scores: Sequence[TierScore]) -> tuple[float | None, float | None]:
    """总体（三档合并）的 precision / recall：按边数加权，不是三个比率的平均。"""

    tp = sum(len(score.tp) for score in scores)
    produced = sum(score.produced_count for score in scores)
    expected = sum(score.expected_count for score in scores)
    return (
        (tp / produced if produced else None),
        (tp / expected if expected else None),
    )


# ───────────────────────────────────────────────────────── 报告


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _short(key: EdgeKey) -> str:
    return f"{key[0]} › {key[1]} → {key[2]} › {key[3]}"


def render_report(
    labels: LabelSet,
    corpora: Mapping[str, TierCorpus],
    scores: Mapping[str, TierScore],
    rows: Mapping[str, Sequence[FormRow]],
    decision: Decision,
) -> str:
    """把三档结果渲染成 ``eval/graph-labels/report.md``（整份生成，手改会被覆盖）。"""

    tiers = list(corpora)
    lines: list[str] = []
    add = lines.append

    add("# 图谱引用边：真实文档上的 precision / recall（#158）")
    add("")
    add(
        "> 本文件由 `scripts/eval_graph_labels.py --write` 生成，**不要手改**："
        "数字与判据都来自 `eval/graph-labels/labels.json` 与 README。"
    )
    add("")
    add("**结论先说**")
    add("")
    add(
        f"- 总体：precision **{_ratio(decision.precision)}**、recall **{_ratio(decision.recall)}**。"
    )
    add(f"- 判定：{decision.reason}。")
    if decision.introduce_llm:
        add(
            "- 为什么不是「再调调正则就行」：漏掉的不是少数写法，而是真实工程文档**指节的语法本身**"
            "（`ADR-0025 决策五`、`见决策 4`、`见文末「后续修订（第 7 步）」`、`见背景四条`）。"
            "这些表达里的定位符是**标题或决策序号**，不是「第 N 节」。"
        )
    else:
        add(
            "- 为什么规则够用：漏掉的 "
            f"**{missing_total(labels, corpora, scores)}** 条里，没有一条是「意思没读懂」——"
            "全是**定位符本身落不到唯一一节**或**被守卫主动挡住**的：`见翻案条件`×2 与 `本文「后果」`×2 "
            "指的两个名字在本文各有两节（唯一性守卫弃权）、`见背景四条` 是两字标题（短名保护）、"
            "`Section 1 of the SLA terms` 指另一份文档（本目录解析不到）。"
        )
    add("")

    add("## 一、按文档形态分档（票面第 2 条：不许只报总体）")
    add("")
    add("| 档 | 源文档 | 标注条数 | 其中 `explicit` | 建出边 | TP | FP | FN | precision | recall |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for tier in tiers:
        score = scores[tier]
        items = labels.of_tier(tier)
        explicit = sum(1 for item in items if item.label == "explicit")
        add(
            f"| {labels.tiers[tier]['label']} | {len(corpora[tier].sections)} 篇里 "
            f"{len(labels.source_documents(tier))} 篇 | {len(items)} | {explicit} | "
            f"{score.produced_count} | {len(score.tp)} | {len(score.fp)} | "
            f"{len(score.fn)} | {_ratio(score.precision)} | {_ratio(score.recall)} |"
        )
    tp = sum(len(score.tp) for score in scores.values())
    fp = sum(len(score.fp) for score in scores.values())
    fn = sum(len(score.fn) for score in scores.values())
    produced = sum(score.produced_count for score in scores.values())
    expected = sum(score.expected_count for score in scores.values())
    add(
        f"| **总体** | — | {len(labels.items)} | {expected} | {produced} | {tp} | {fp} | "
        f"{fn} | {_ratio(decision.precision)} | {_ratio(decision.recall)} |"
    )
    add("")
    add(
        "口径：只数 `reference` 边（兄弟 / 父节边不来自正文）；源文档之外的文档只进目录、"
        "充当解析目标，它们的出边不进分母。分母为 0 时 precision 记「—」而不是 0："
        "「一条边都没建」与「建了全错」是两件事。"
    )
    add("")

    add("## 二、按表达式形态（票面第 2 条：形态分解比总体更有信息量）")
    add("")
    add(
        "四列的含义：**条数** = 标注条数；**候选器认出** = 生产抽取器的候选识别（正则那一步）在"
        "这一处报过候选；**边对上了** = 抽出的边与标注的四元组一致；**认出且对上** = 再加上"
        "「是因为认出了这一处表达」——**边对上了但不在这一列**的，是撞对（见第四节）。"
    )
    add("")
    for tier in tiers:
        add(f"**{labels.tiers[tier]['label']}**")
        add("")
        add("| 形态 | 标签类 | 条数 | 候选器认出 | 边对上了 | 认出且对上 |")
        add("| --- | --- | --- | --- | --- | --- |")
        for row in rows[tier]:
            add(
                f"| {row.form} | {row.label} | {row.items} | {row.recognized} | "
                f"{row.matched} | {row.exact} |"
            )
        add("")

    add("## 三、漏掉的那 %d 条分两种：识别侧 vs 解析侧" % missing_total(labels, corpora, scores))
    add("")
    add(
        "这一节是「补正则值不值」的判据。**识别侧**（候选器根本没认出来）需要新的表达规则，"
        "而且要一条条穷举真实写法；**解析侧**（认出来了但目标落不下去）是几十行代码就能补的口径"
        "（标题编号形态、章-节双层、条/款后缀）。"
    )
    add("")
    add("| 档 | 标注 | 漏在哪一侧 | 表达 |")
    add("| --- | --- | --- | --- |")
    sides: dict[str, int] = {}
    for tier in tiers:
        for item, side in unmatched_explicit(corpora[tier], labels.of_tier(tier), scores[tier]):
            sides[side] = sides.get(side, 0) + 1
            add(f"| {labels.tiers[tier]['label']} | {item.id} | {side} | `{item.evidence}` |")
    add("")
    add(
        "合计："
        + "、".join(f"{side} **{count}** 条" for side, count in sorted(sides.items()))
        + "。"
    )
    add("")

    add("## 四、撞对：边是对的，但不是因为认出了这条表达")
    add("")
    add(
        "这几条最容易被自测骗过去：语料对账会全绿，因为边真的在；但把它当成「抽取器会读这种写法」"
        "的证据就是错的——它读到的其实是同一个节里另一处表达。"
    )
    add("")
    lucky = [
        (tier, item)
        for tier in tiers
        for item in lucky_hits(corpora[tier], labels.of_tier(tier), scores[tier])
    ]
    if not lucky:
        add("（本测量集里没有）")
    else:
        add("| 档 | 标注 | 标注的表达 | 说明 |")
        add("| --- | --- | --- | --- |")
        for tier, item in lucky:
            add(f"| {labels.tiers[tier]['label']} | {item.id} | `{item.evidence}` | {item.note} |")
    add("")

    add("## 五、误报明细（每条都说清它凭什么是错的）")
    add("")
    add("| 档 | 边 | 归因 |")
    add("| --- | --- | --- |")
    for tier in tiers:
        for key in scores[tier].fp:
            label, identifier = scores[tier].attribution.get(key, ("未标注", ""))
            add(f"| {labels.tiers[tier]['label']} | `{_short(key)}` | {label} {identifier} |")
    add("")

    add("## 六、噪声专项（提到但不依赖：「已废弃 / 已作废 / 格式示例」）")
    add("")
    noise_items = [item for item in labels.items if item.label == "noise"]
    add("| 档 | 标注 | 形态 | 原文 | 候选器认出 | 结果 |")
    add("| --- | --- | --- | --- | --- | --- |")
    noise_fp = 0
    for tier in tiers:
        for item, seen, outcome in noise_rows(corpora[tier], labels.of_tier(tier), scores[tier]):
            noise_fp += int(outcome == "误报")
            add(
                f"| {labels.tiers[tier]['label']} | {item.id} | {item.form_detail or item.form} | "
                f"`{item.evidence}` | {'是' if seen else '否'} | {outcome} |"
            )
    add("")
    add(f"标注集里 `noise` 共 **{len(noise_items)}** 条；被计成误报 **{noise_fp}** 条。结论：")
    add("")
    add(
        "1. **格式示例会真的建出边，而不是理论风险。** ADR-0027 里那句"
        "「「参见《X》第 2.4 节」「本文件第三节」、Markdown 链接、同文档锚点」是表格的"
        "「例子」格，抽取器照样把它当成作者的引用；这次它建出的边恰好与另一条真标注重合"
        "（撞对），**没被计成误报**。撞对是巧合：同一个节里没有「第 3 节」时它就会长成一条假引用。"
        "**代码块里的「写法 → 目标」对照行已被两条守卫挡掉**（围栏代码块 + 同一行出现 `→`），"
        "本次不再从决策 8 的例子表里建边；但**表格单元格**里的例子仍按作者的引用处理。"
    )
    add(
        "2. **否定语境（已作废 / 已关闭 / 不再追踪 / 已取代）**：已在「产生候选之后、解析之前」"
        "用否定词清单挡掉（`已[^，。；]{0,4}(作废|废止|废弃|关闭|取代)`、`不再追踪/适用`、"
        "`仅供参考`），本档 `deprecated_context` 不再建边。"
    )
    add(
        "3. **「已废止的旧文档」这一类反而不会长成误报**：`原《优惠券使用细则》第 2 条已废止` "
        "的目标文档不在库里，抽取器解析不到目标，一条边都不建。所以**否定词清单要挡的是上面那两种"
        "（格式示例、语境否定），不是「文档已废止」**——后者由「目标必须存在」这条规则天然挡住。"
    )
    add("")

    add("## 七、每一档能外推到哪些形态")
    add("")
    for tier in tiers:
        meta = labels.tiers[tier]
        add(
            f"**{meta['label']}**（{len(corpora[tier].sections)} 篇进目录，"
            f"{len(labels.source_documents(tier))} 篇被标注）"
        )
        add("")
        add(f"- 取材：{meta['source_selection']}")
        add(f"- 可外推到：{meta['extrapolable_to']}")
        add(f"- **不可**外推到：{meta['not_extrapolable_to']}")
        add("")

    add("## 八、顺带发现（与本票的结论无关，但不该被埋掉）")
    add("")
    for note in INCIDENTAL_FINDINGS:
        add(f"- {note}")
    add("")
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────── 入口


def run(root: Path | None = None) -> tuple[LabelSet, dict[str, TierCorpus], dict[str, TierScore]]:
    """跑完三档，返回标注集、语料与比分（供用例与 CLI 共用）。"""

    base = root or Path(__file__).resolve().parents[1]
    labels = load_labels(base / "eval" / "graph-labels" / "labels.json")
    corpora: dict[str, TierCorpus] = {}
    scores: dict[str, TierScore] = {}
    for tier in labels.tiers:
        corpus = build_corpus(labels, tier, base)
        items = labels.of_tier(tier)
        score = reconcile(
            produced_edges(corpus, labels.source_documents(tier)),
            expected_edges(corpus, items),
            items,
            corpus,
        )
        corpora[tier] = corpus
        scores[tier] = score
    return labels, corpora, scores


def main(argv: Sequence[str] | None = None) -> int:
    # 延迟取实现：缺它就是「测量对象已被撤销」，印 tag 指针并退出 0（不是崩）。
    _load_rejected_scheme()
    args = list(argv if argv is not None else sys.argv[1:])
    base = Path(__file__).resolve().parents[1]
    labels, corpora, scores = run(base)
    rows = {tier: form_rows(corpora[tier], labels.of_tier(tier), scores[tier]) for tier in scores}
    precision, recall = pooled([scores[tier] for tier in scores])
    decision = decide(recall, precision)
    report = render_report(labels, corpora, scores, rows, decision)
    if "--write" in args:
        (base / "eval" / "graph-labels" / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
