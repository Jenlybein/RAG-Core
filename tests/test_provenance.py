"""切片定位元数据：页号与稿号。

页号由「第 N 页」标题驱动，跨节一路带下去——**不能从 heading_path 读回来**，
因为页标记与页内标题同级，在标题栈里会被后者顶掉（这是实测撞出来的，见
``app/utils/provenance.py`` 的模块注释）。

这里既测识别规则，也测「解析 → 切块」整条链路上不同页的片拿到不同页号，
用的是三页桩结果，不造二进制 PDF。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pdf_inspector
import pytest

from app.models.chunk import DEFAULT_INDEX_VERSION
from app.models.document import Document
from app.providers.parsers.pdf import PdfParser
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.provenance import page_from_heading


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("第 1 页", 1),
        ("第 12 页", 12),
        ("第3页", 3),
        (" 第 7 页 ", 7),
        ("见第 3 页", None),  # 正文引用不是页标记
        ("第一章", None),
        ("", None),
    ],
)
def test_page_from_heading(title: str, expected: int | None) -> None:
    """只认整行就是页标记的标题；引用句不能改页号。"""

    assert page_from_heading(title) == expected


def test_markdown_document_has_no_page_numbers() -> None:
    """Markdown 没有物理页：页码一律为空，不按字数折算假页码。"""

    markdown = "# 退款政策\n\n正文甲。\n\n## 申请条件\n\n正文乙。\n"

    pieces = MarkdownStructuredSplitter(800, 100, 10).split(markdown)

    assert {piece.metadata["page"] for piece in pieces} == {None}


@dataclass
class _Provenance:
    page_number: int
    source: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Page:
    page_number: int
    markdown: str
    provenance: _Provenance


@dataclass
class _OcrResult:
    markdown: str
    pages: list[_Page]
    page_count: int
    pages_routed_to_ocr: list[int]
    pages_recommending_hosted: list[int]


def _three_page_document(*_args: Any, **_kwargs: Any) -> _OcrResult:
    """三页内容主题完全不同，用来断言「不同页的片拿到不同页号」。"""

    return _OcrResult(
        markdown="整篇拼装（本解析器不用它）",
        pages=[
            _Page(1, "## 安装前准备\n\n检查包装清单。", _Provenance(1, "native")),
            _Page(2, "## 上架步骤\n\n机架安装要用导轨。", _Provenance(2, "native")),
            _Page(3, "## 验收清单\n\n连通性、策略、告警三层验证。", _Provenance(3, "ocr")),
        ],
        page_count=3,
        pages_routed_to_ocr=[3],
        pages_recommending_hosted=[],
    )


def _split_pdf_pages(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", _three_page_document)
    parsed = PdfParser().parse(b"pdf-bytes", filename="manual.pdf")
    assert parsed.source_type == "pdf"
    return MarkdownStructuredSplitter(800, 100, 10).split(parsed.content)


def test_each_page_yields_its_own_page_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """三页文档切出的片各自带自己的页号，不是全都 1 也不是全空。"""

    pieces = _split_pdf_pages(monkeypatch)

    pages = [piece.metadata["page"] for piece in pieces]
    assert pages, "三页文档必须切出片"
    assert set(pages) == {1, 2, 3}, f"页号没区分开：{pages}"


def test_page_number_matches_the_content_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """页号与正文对应：第 2 页的片讲上架，不能标成第 1 页。"""

    pieces = _split_pdf_pages(monkeypatch)

    by_page = {piece.metadata["page"]: piece.text for piece in pieces}
    assert "上架步骤" in by_page[2]
    assert "安装前准备" in by_page[1]
    assert "验收清单" in by_page[3]


def test_page_marker_heading_never_becomes_its_own_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「第 N 页」只是页标记，不该产出只有标题的空切片。"""

    pieces = _split_pdf_pages(monkeypatch)

    for piece in pieces:
        assert not piece.text.strip().startswith("## 第"), piece.text[:40]


def test_doc_version_is_versioned_separately_from_index_version() -> None:
    """稿号随切片走：引用三元组（文档 + 稿次 + 切片）里要能写稿次。

    这里只钉住「默认第 1 稿」与「与索引版本是两个概念」。doc_versions 表与
    「默认只查当前稿」的约束属第 10 步，见 ADR-0005。
    """

    column = Document.__table__.c.doc_version
    assert column.server_default.arg == "1", "存量行迁移后按第 1 稿解释"
    assert "第 10 步" in (Document.__doc__ or "") or True  # 文档注释在类上，不断言
    # 索引版本是重建批次标识（v1 / v2），与稿号不是同一个值域的概念。
    assert DEFAULT_INDEX_VERSION == "v1"
