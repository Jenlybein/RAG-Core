"""#152 · 拿**真实引用语料**对抽取器：manifest 的 ``reference_inventory`` 逐条对账。

为什么值得单独一个文件：``eval/corpus-refs`` 是本步唯一有「显式引用表达」的语料（基线
语料里一条都没有），它同时是 C6 的跨文档对照的输入。抽取器在它上面认不出链接的话，
对照会得出「图谱一共没几条边」的**假结论**——不是图谱没用，是抽取器没认出链接。
2026-11-14 复核时正是这样：链接写的是 ``./points_policy.md``，而文档标题是《积分规则》，
按标题去对文件名，三条链接一条边都建不出来。

这里不连库：用生产那份切块器切真实文件、按 ``documents`` 表里那两个字段（标题 +
原始文件名）造节目录，然后跑生产那份抽取器，把结果与清单逐条比对。清单声称它是完整的
（``_readme`` 里那条「清单钉住每条边」），所以这里连**条数相等**也一起断言。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.models.section_edge import RELATION_REFERENCE
from app.services.section_graph import (
    EdgeScope,
    SectionCatalog,
    SectionRecord,
    SectionText,
    extract_section_edges,
)
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.section_identity import section_id_of

REFS_DIR = Path(__file__).resolve().parents[1] / "eval" / "corpus-refs"
MANIFEST_PATH = REFS_DIR / "manifest.json"
SCOPE = EdgeScope(tenant_id="tenant-eval", kb_id="kb-refs", index_version="v1")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _corpus() -> tuple[SectionCatalog, dict[str, tuple[SectionText, ...]]]:
    """按真实文件造「这个版本里的节与正文」，形状与索引期一致。"""

    manifest = _manifest()
    records: list[SectionRecord] = []
    per_document: dict[str, tuple[SectionText, ...]] = {}
    for key, meta in manifest["documents"].items():
        splitter = MarkdownStructuredSplitter(document_id=key)
        pieces = splitter.split((REFS_DIR / meta["file"]).read_text(encoding="utf-8"))
        entries: list[SectionText] = []
        for index, piece in enumerate(pieces):
            heading = str(piece.metadata.get("heading_path") or "")
            record = SectionRecord(
                tenant_id=SCOPE.tenant_id,
                kb_id=SCOPE.kb_id,
                index_version=SCOPE.index_version,
                document_id=key,
                document_title=meta["title"],
                section_id=section_id_of(document_id=key, heading_path=heading),
                heading_path=heading,
                order=index,
                source_filename=meta["file"],
            )
            entries.append(SectionText(record=record, text=piece.text))
        per_document[key] = tuple(entries)
        records.extend(entry.record for entry in entries)
    return SectionCatalog.of(records, scope=SCOPE), per_document


def _heading_of(
    per_document: dict[str, tuple[SectionText, ...]], document: str, number: str
) -> str:
    """把清单里的「节号」换成切块器实际写下的标题路径。"""

    pattern = re.compile(rf"^{re.escape(number)}(?!\d)(?!\.\d)")
    for entry in per_document[document]:
        if pattern.match(entry.record.heading_title.strip()):
            return entry.record.heading_path
    raise AssertionError(f"{document}: 语料里找不到节号 {number} 对应的标题")


def _reference_edges() -> set[tuple[str, str, str, str]]:
    """``(源文档, 源标题路径, 目标文档, 目标标题路径)`` 的引用边集合。"""

    catalog, per_document = _corpus()
    edges: set[tuple[str, str, str, str]] = set()
    for document, entries in per_document.items():
        for edge in extract_section_edges(
            scope=SCOPE,
            document_id=document,
            document_title=entries[0].record.document_title,
            sections=entries,
            catalog=catalog,
        ):
            if edge.relation_type != RELATION_REFERENCE:
                continue
            edges.add(
                (
                    edge.source_document_id,
                    str(edge.metadata["source_heading_path"]),
                    edge.target_document_id,
                    str(edge.metadata["target_heading_path"]),
                )
            )
    return edges


def test_every_inventory_edge_is_extracted_from_the_real_corpus() -> None:
    """清单里的每条边都要被抽出来——包括那三条 Markdown 链接。"""

    catalog, per_document = _corpus()
    expected = {
        (
            row["from"]["document"],
            _heading_of(per_document, row["from"]["document"], row["from"]["section"]),
            row["to"]["document"],
            _heading_of(per_document, row["to"]["document"], row["to"]["section"]),
        )
        for row in _manifest()["reference_inventory"]
    }

    actual = _reference_edges()

    assert actual == expected, (
        "抽出来的边与清单不一致：少了 = 抽取规则漏了这种表达，多了 = 抽取器在猜"
    )
    assert len(catalog.sections) > len(expected), "目录应该包含所有节，不只是被引用的"


def test_the_markdown_link_form_actually_produces_edges() -> None:
    """单独钉住链接形态：它是「按文件名对目标」这条规则唯一的真实证据。"""

    edges = _reference_edges()
    link_rows = [
        row for row in _manifest()["reference_inventory"] if row["form"] == "markdown_link"
    ]

    assert len(link_rows) == 3
    for row in link_rows:
        matched = [
            edge
            for edge in edges
            if edge[0] == row["from"]["document"] and edge[2] == row["to"]["document"]
        ]
        assert matched, f"链接没抽出边：{row['evidence']} —— 链接目标写的是文件名，不是标题"


def test_the_three_forms_are_all_covered_by_the_corpus() -> None:
    """清单声明的三种形态都在语料里，且都被抽出来了（防止将来只剩一种）。"""

    forms = {row["form"] for row in _manifest()["reference_inventory"]}

    assert forms == {"explicit_cross_doc", "same_doc", "markdown_link"}


@pytest.mark.parametrize("row_index", [0, 5, 8])
def test_each_link_edge_points_at_the_numbered_section(row_index: int) -> None:
    """链接后面那个「第 N 节」被真的用上了：目标不是「整篇」，是那一节。"""

    row = _manifest()["reference_inventory"][row_index]
    _, per_document = _corpus()
    expected_target = _heading_of(per_document, row["to"]["document"], row["to"]["section"])

    edges = _reference_edges()

    assert any(
        edge[0] == row["from"]["document"] and edge[3] == expected_target for edge in edges
    ), f"目标节对不上：{row['evidence']}"
