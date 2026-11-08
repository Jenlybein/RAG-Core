"""PDF 结构还原：标题层级、表格、多页页号、端到端切块。

fixture `tests/fixtures/ocr/structured_pages.pdf` 由 `scripts/make_pdf_fixture.py`
生成，是**原生文字层**的多页 PDF（3 页、含 h1/h2 与一张 4 列 3 行的表），所以这些
用例不需要 PDFium / ONNX Runtime / 模型，默认 `pytest -q` 就会跑。

与 `test_ocr_contract.py` 的分工：那边钉的是「运行时有没装、失败怎么分类」，
这边钉的是「解析出来的结构对不对」。

末尾两条标了 `known limitation` 的用例断言的是**当前的真实缺陷**，不是理想行为。
修好时会变红，那是提示把断言改成正确期望——别直接删。
"""

from __future__ import annotations

import pathlib

import pytest

from app.providers.parsers.pdf import PdfParser
from app.utils.markdown_splitter import MarkdownStructuredSplitter

FIXTURE = pathlib.Path(__file__).parent / "fixtures/ocr/structured_pages.pdf"


@pytest.fixture(scope="module")
def parsed_content() -> str:
    return PdfParser().parse(FIXTURE.read_bytes(), filename="structured.pdf").content


@pytest.fixture(scope="module")
def pieces(parsed_content: str):
    return MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100).split(parsed_content)


def test_native_extraction_needs_no_ocr_round_trip() -> None:
    """三页都是原生文字层：不路由到 OCR，也不推荐托管 OCR。"""

    parsed = PdfParser().parse(FIXTURE.read_bytes(), filename="structured.pdf")

    assert parsed.metadata["page_count"] == 3
    assert parsed.metadata["engine"] == ["native"]
    assert parsed.metadata["ocr_pages"] == []


def test_every_page_gets_a_page_marker(parsed_content: str) -> None:
    """页标记是三页各一个 h2，供 `page_from_heading` 读页号。"""

    for number in (1, 2, 3):
        assert f"## 第 {number} 页" in parsed_content


def test_heading_levels_follow_font_size(parsed_content: str) -> None:
    """22pt 的文档标题成 h1、15pt 的节标题成 h2——结构来自版式，不是靠猜。"""

    assert "# 部署实施指南" in parsed_content
    assert "## 上架与供电" in parsed_content


def test_table_becomes_a_markdown_table(parsed_content: str) -> None:
    """表格被还原成 `|` 语法——这是行级表片能够产出片的前提。"""

    assert "|型号|千兆网口|万兆光口|转发吞吐|" in parsed_content
    assert "|---|---|---|---|" in parsed_content
    assert "|XR-2100|8|2|2.4 Gbps|" in parsed_content


def test_table_yields_whole_table_plus_row_level_pieces(pieces) -> None:
    """一张表产出 1 条整表片 + 每个数据行各一条行级片，行级片的正文自带列名。"""

    tables = [p for p in pieces if p.metadata.get("chunk_type") == "table"]
    rows = [p for p in pieces if p.metadata.get("chunk_type") == "table_row"]

    assert len(tables) == 1
    assert len(rows) == 3, "表头不算数据行"
    assert any("型号=XR-2100" in p.text for p in rows)
    assert all("型号=" in p.text for p in rows), "行级片必须带列名，脱离表头也读得懂"


def test_chunks_keep_heading_path_and_page(pieces) -> None:
    """每片都能说出自己在哪一节、哪一页——引用要能落到位置上。"""

    assert all(p.metadata.get("heading_path") for p in pieces)
    assert {p.metadata.get("page") for p in pieces} == {1, 2, 3}
    assert any(p.metadata.get("heading_path") == "部署实施指南 > 上架与供电" for p in pieces)


def test_known_limitation_h1_restarts_on_every_page(parsed_content: str) -> None:
    """已知限制：每页的第一个标题都会被识别成 h1，跨页层级不延续。

    实测：第 2 页的「整机规格」、第 3 页的「常见问题」在原文里都是 15pt 的 h2，
    但 pdf-inspector 逐页独立识别，把它们输出成了 `#`。后果是 `heading_path` 里
    拿不到文档级父级（`整机规格` 而不是 `部署实施指南 > 整机规格`），多页文档的
    跨页归属因此会断。

    修好之后本断言应变红——那时改成断言 `## 整机规格` 并把本条改名去掉 limitation。
    """

    assert "# 整机规格" in parsed_content
    assert "# 常见问题" in parsed_content


def test_known_limitation_one_body_line_is_misread_as_heading(parsed_content: str) -> None:
    """已知限制：一句 10pt 正文被识别成了 `##` 标题。

    「表中吞吐为开启基本功能后的实测值，开启 VPN 后按比例衰减。」是正文，却被
    打上了标题标记。后果是它自成一节：它之后的正文都会被挂到这句"标题"下面，
    `heading_path` 里也因此出现了句子而不是章节名。

    这是版式识别的误判，不是我们拼装的问题。修好（或换引擎）后本断言应变红。
    """

    assert "## 表中吞吐为开启基本功能后的实测值，开启 VPN 后按比例衰减。" in parsed_content
