"""#158 · 标注集与测量口径：**先钉口径，再信数字**。

这一份用例守的不是「抽取器抽得对」，而是**测量本身站得住**——一个算错口径的测量比没有测量更糟，
因为它给出的是一个更可信的假数字。所以这里分两层：

1. **纯函数的真值表**（``decide`` / ``pooled`` / ``reconcile``）：期望值全部**手算**，不是把实现
   再跑一遍。阈值那几条直接抄票面（recall ≥ 0.7 且 precision ≥ 0.95）。
2. **标注集自身的完整性**（``labels.json``）：每条证据落在唯一一个节里、每条 ``explicit`` 的目标
   节在目录里恰好一处、四类标签与六类形态的取值合法、报告与当前状态逐字一致。

第 2 层用真实语料（本仓库的 ADR + 两档合成语料）跑生产那份切块器与抽取器，所以它同时是
«抽取器在这份测量集上的读数没有漂» 的守卫：有人改了抽取规则、报告没重跑，这条会红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.section_edge import RELATION_REFERENCE
from app.services.section_graph import (
    EdgeDraft,
    EdgeScope,
    SectionCatalog,
    SectionRecord,
    SectionText,
)
from scripts.eval_graph_labels import (
    PRECISION_FLOOR,
    RECALL_FLOOR,
    LabelItem,
    TierCorpus,
    TierScore,
    build_corpus,
    decide,
    edge_key,
    expected_edges,
    form_rows,
    load_labels,
    pooled,
    reconcile,
    recognition,
    render_report,
    resolve_target,
    run,
    section_of_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
LABELS_PATH = ROOT / "eval" / "graph-labels" / "labels.json"
REPORT_PATH = ROOT / "eval" / "graph-labels" / "report.md"


# ───────────────────────────────────────────── 纯函数：阈值与合并口径（手算真值表）


@pytest.mark.parametrize(
    ("recall", "precision", "introduce_llm"),
    [
        # 票面：recall ≥ 0.7 且 precision ≥ 0.95 → 不上 LLM。边界值取等号成立。
        (0.7, 0.95, False),
        (0.9, 1.0, False),
        (0.69, 1.0, True),
        (1.0, 0.94, True),
        (0.0, 1.0, True),
        (1.0, 0.0, True),
        # 一条边都没建（分母 0）不是「完美」，是「没测到」：同样不能据此说不用上 LLM。
        (None, 1.0, True),
        (1.0, None, True),
    ],
)
def test_decision_thresholds(
    recall: float | None, precision: float | None, introduce_llm: bool
) -> None:
    assert decide(recall, precision).introduce_llm is introduce_llm


def test_thresholds_are_the_ones_from_the_ticket() -> None:
    """票面数字写死在常量里；改它等于改验收标准，必须是一次显式决定。"""

    assert RECALL_FLOOR == 0.7
    assert PRECISION_FLOOR == 0.95


def test_pooled_weights_by_edges_not_by_tiers() -> None:
    """A 档 1/3（建 3 条对 1 条）、B 档 0 条边：合并是 1/3，不是 (0.33+0)/2。

    A 档另有 10 条真边未建，B 档 30 条：recall = 1 / 40。
    """

    a = TierScore(
        "a",
        tp=[("t", "s", "t", "s2")],
        fp=[(), ()],
        fn=[],
        expected_count=10,
        produced_count=3,
        false_negative_ids={},
        attribution={},
    )
    b = TierScore(
        "b",
        tp=[],
        fp=[],
        fn=[],
        expected_count=30,
        produced_count=0,
        false_negative_ids={},
        attribution={},
    )

    assert pooled([a, b]) == pytest.approx((1 / 3, 1 / 40))


def test_tier_with_no_edges_reports_none_not_zero() -> None:
    """「一条边都没建」与「建了全错」是两件事：前者 precision 是 None。"""

    score = TierScore(
        "x",
        tp=[],
        fp=[],
        fn=[],
        expected_count=5,
        produced_count=0,
        false_negative_ids={},
        attribution={},
    )

    assert score.precision is None
    assert score.recall == 0.0


# ───────────────────────────────────────────── 纯函数：比对与归因（手搭最小语料）


def _record(document: str, title: str, heading: str, order: int) -> SectionRecord:
    return SectionRecord(
        tenant_id="t",
        kb_id="kb",
        index_version="v1",
        document_id=document,
        document_title=title,
        section_id=f"{document}:{heading}",
        heading_path=heading,
        order=order,
        source_filename=f"{document}.md",
    )


def _mini_corpus() -> TierCorpus:
    scope = EdgeScope(tenant_id="t", kb_id="kb", index_version="v1")
    first = _record("doc-a", "A 文档", "A 文档 > 一", 0)
    second = _record("doc-a", "A 文档", "A 文档 > 二", 1)
    target = _record("doc-b", "B 文档", "B 文档 > 一", 0)
    return TierCorpus(
        tier="mini",
        scope=scope,
        catalog=SectionCatalog.of([first, second, target], scope=scope),
        sections={
            "doc-a": (
                SectionText(first, "见《B 文档》第 1 节。"),
                SectionText(second, "详见《B 文档》第 2 节，已作废不动。"),
            ),
            "doc-b": (SectionText(target, "内容"),),
        },
    )


def _edge(source: SectionRecord, target: SectionRecord, expression: str) -> EdgeDraft:
    return EdgeDraft(
        tenant_id=source.tenant_id,
        kb_id=source.kb_id,
        index_version=source.index_version,
        source_document_id=source.document_id,
        source_section_id=source.section_id,
        target_document_id=target.document_id,
        target_section_id=target.section_id,
        relation_type=RELATION_REFERENCE,
        metadata={
            "expression": expression,
            "source_heading_path": source.heading_path,
            "target_heading_path": target.heading_path,
        },
    )


def test_reconcile_scores_one_true_edge_and_one_false_positive() -> None:
    """手算：1 条真边建对、1 条在 noise 语境里建错 → precision 1/2，recall 1/1。"""

    corpus = _mini_corpus()
    first, second = corpus.sections["doc-a"][0].record, corpus.sections["doc-a"][1].record
    target = corpus.sections["doc-b"][0].record
    items = (
        LabelItem(
            id="e1",
            tier="mini",
            document="doc-a",
            evidence="见《B 文档》第 1 节",
            label="explicit",
            form="title_section",
            target_document="doc-b",
            target_section="一",
            target_path="B 文档 > 一",
        ),
        LabelItem(
            id="n1",
            tier="mini",
            document="doc-a",
            evidence="已作废不动",
            label="noise",
            form="other",
        ),
    )
    produced = (
        _edge(first, target, "《B 文档》第 1 节"),
        _edge(second, target, "《B 文档》第 2 节"),
    )

    score = reconcile(produced, expected_edges(corpus, items), items, corpus)

    assert score.produced_count == 2
    assert len(score.tp) == 1
    assert len(score.fp) == 1
    assert len(score.fn) == 0
    assert score.precision == 0.5
    assert score.recall == 1.0
    assert set(score.attribution.values()) == {("noise", "n1")}


def test_reconcile_counts_a_wrong_target_as_false_positive() -> None:
    """「方向对、粒度错」也是错：建到同一篇的另一个节上，既不是 TP 也不是漏。"""

    corpus = _mini_corpus()
    first, second = corpus.sections["doc-a"][0].record, corpus.sections["doc-a"][1].record
    items = (
        LabelItem(
            id="e1",
            tier="mini",
            document="doc-a",
            evidence="见《B 文档》第 1 节",
            label="explicit",
            form="title_section",
            target_document="doc-b",
            target_section="一",
        ),
    )
    produced = (_edge(second, first, "《B 文档》第 1 节"),)

    score = reconcile(produced, expected_edges(corpus, items), items, corpus)

    assert score.produced_count == 1
    assert score.tp == ()
    assert len(score.fp) == 1
    assert len(score.fn) == 1


# ───────────────────────────────────────────── 标注集自身：证据与目标都要唯一


@pytest.fixture(scope="module")
def measured() -> tuple:
    return run(ROOT)


def test_every_evidence_lands_in_exactly_one_section(measured: tuple) -> None:
    labels, corpora, _ = measured
    for item in labels.items:
        section = section_of_evidence(corpora[item.tier], item)
        assert item.evidence in section.text
        # 标注里写的来源节必须与切块器算出来的一致（标注不是手抄的自由文本）。
        if item.source_section_path is not None:
            assert section.record.heading_path == item.source_section_path, item.id
        else:
            assert section.record.heading_title == item.source_section, (
                f"{item.id}: 标注写的来源节 {item.source_section!r} 与切块器算出的 "
                f"{section.record.heading_title!r} 不一致"
            )


def test_every_explicit_label_has_a_resolvable_target(measured: tuple) -> None:
    labels, corpora, _ = measured
    for item in labels.items:
        if item.label == "explicit":
            assert item.target_document and item.target_section, item.id
            resolve_target(corpora[item.tier], item)  # 对不上会抛
        else:
            assert item.target_document is None and item.target_section is None, (
                f"{item.id}: 非 explicit 的标注不该有目标（它本来就该弃权）"
            )


def test_label_set_covers_the_ticket_requirements(measured: tuple) -> None:
    """票面：≥50 处引用、≥3 种文档形态、四类标签都要有。"""

    labels, _, _ = measured

    assert len(labels.items) >= 50
    assert set(labels.tiers) == {"engineering", "policy", "minutes"}
    assert {item.label for item in labels.items} == {"explicit", "ambiguous", "implicit", "noise"}
    assert {item.form for item in labels.items} >= {
        "title_section",
        "same_doc",
        "markdown_link",
        "anchor",
        "chinese_ordinal",
        "other",
    }


def test_measurement_corpus_keeps_the_training_corpus_out(measured: tuple) -> None:
    """``eval/corpus-refs`` 是抽取器的「训练题」，一条都不许进测量集。"""

    labels, corpora, _ = measured
    for corpus in corpora.values():
        for path in corpus.documents.values():
            assert "corpus-refs" not in path.replace("\\", "/")
    for path in labels.documents.values():
        assert "corpus-refs" not in path.replace("\\", "/")


def test_engineering_tier_measures_real_adr_documents(measured: tuple) -> None:
    """工程档必须是仓库里真实存在的 ADR（不是自造语料）——这是整份测量的立身之本。"""

    labels, corpora, _ = measured
    for key in labels.source_documents("engineering"):
        path = ROOT / labels.documents[key]
        assert path.is_file()
        assert path.parent == ROOT / "docs" / "adr"
    assert len(corpora["engineering"].catalog.sections) > 100, (
        "工程档的目录应当包含整簇 ADR 的节（只放三篇会让跨文档引用无处解析）"
    )


def test_recognition_is_specific_to_the_expression(measured: tuple) -> None:
    """候选识别按「这一处表达」算，不按节算：**认出的是定位符，不是这一节**。

    #158 决策 8 给 R1 / R2 补了候选形态之后，这条要从「这种写法不在规则里」翻过来：
    ADR-0025 的决策序号写法现在认得出；同一个「决策 N」词放在陈述句里（没有引用框架）
    仍然不认；而表格里的格式示例照旧会被认成引用（候选识别不看语境）。
    """

    labels, corpora, _ = measured
    item = next(row for row in labels.items if row.id == "eng-0027-02")
    statement = next(row for row in labels.items if row.id == "eng-0027-17")
    other = next(row for row in labels.items if row.id == "eng-0027-05")

    assert recognition(corpora["engineering"], item) is True  # ADR-0025 决策五：R2 认出来了
    assert recognition(corpora["engineering"], statement) is False  # 陈述句里的决策号：没有引用框架
    assert recognition(corpora["engineering"], other) is True  # 表格里的格式示例：会被认成引用


def test_report_matches_the_current_state(measured: tuple) -> None:
    """报告是生成物：改了标注或抽取规则却没重跑报告，这条会红。"""

    labels, corpora, scores = measured

    rows = {tier: form_rows(corpora[tier], labels.of_tier(tier), scores[tier]) for tier in scores}
    precision, recall = pooled([scores[tier] for tier in scores])
    expected = render_report(labels, corpora, scores, rows, decide(recall, precision))

    assert REPORT_PATH.read_text(encoding="utf-8") == expected, (
        "eval/graph-labels/report.md 与当前标注集 / 抽取器不一致："
        "跑 `uv run python scripts/eval_graph_labels.py --write` 重新生成"
    )


def test_edge_key_is_the_four_tuple_used_for_matching(measured: tuple) -> None:
    """比对键必须带**两端的标题路径**：只带文档 id 会让「同文档不同节」互相顶替。"""

    _, _, scores = measured
    keys = {edge_key(edge) for tier in scores for edge in scores[tier].edges}

    assert keys
    assert all(len(key) == 4 for key in keys)
    assert all(key[1] for key in keys), "源标题路径不能是空串（否则整篇的边全撞在一起）"


def test_build_corpus_titles_come_from_the_first_heading() -> None:
    """标题 = 首个 ``#`` 标题：``《标题》第 N 节`` 对的是它，不是文件名。"""

    labels = load_labels(LABELS_PATH)
    corpus = build_corpus(labels, "minutes", ROOT)

    titles = {record.document_title for record in corpus.catalog.sections}
    assert "产品周会纪要（2026-11-03）" in titles
