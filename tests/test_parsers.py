"""T1 解析层：5 个 Parser、注册表选择与唯一解析入口。

全部离线：mammoth 与 pdf-inspector 一律 monkeypatch，不读真实 docx / pdf、
不加载 PDFium / ONNX Runtime、不下载 OCR 模型、不联网。HTML 走真实解析
（bs4 + markdownify 都是纯 CPU 库，不需要替身）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mammoth
import pdf_inspector
import pytest

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.document import Document
from app.providers.parsers import get_parser_registry
from app.providers.parsers.docx import DocxParser
from app.providers.parsers.html import HtmlParser
from app.providers.parsers.pdf import PdfParser
from app.providers.parsers.text import TextParser
from app.services.document_ingestion import prepare_document_content


class _StubMessage:
    """mammoth 的 messages 是 ``Message(type, message)`` 命名元组。"""

    def __init__(self, type_: str, message: str) -> None:
        self.type = type_
        self.message = message


class _StubMarkupResult:
    def __init__(self, value: str, messages: list[Any] | None = None) -> None:
        self.value = value
        self.messages = messages or []


def test_text_parser_decodes_utf8() -> None:
    parsed = TextParser().parse("纯文本内容".encode(), filename="notes.txt")

    assert parsed.content == "纯文本内容"
    assert parsed.source_type == "text"
    assert parsed.metadata == {"parser": "text"}


def test_docx_parser_emits_atx_headings(monkeypatch: pytest.MonkeyPatch) -> None:
    # ATX 回归：markdownify 默认是 Setext（``标题\n======``），结构化切块器只认
    # ``#`` / ``##``，所以这里必须看到 ATX 而非下划线。
    result = _StubMarkupResult(
        "<h1>退款政策</h1><h2>申请条件</h2>",
        [_StubMessage("warning", "unrecognised style")],
    )
    monkeypatch.setattr(mammoth, "convert_to_html", lambda _stream: result)

    parsed = DocxParser().parse(b"fake-docx", filename="refund.docx")

    assert "# 退款政策" in parsed.content
    assert "## 申请条件" in parsed.content
    assert "=====" not in parsed.content, "Setext 标题会让结构化切块器漏掉整篇章节"
    assert parsed.source_type == "docx"
    assert parsed.metadata["parser"] == "docx"
    assert parsed.metadata["warnings"] == ["warning: unrecognised style"]


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>退款政策 - 帮助中心</title>
  <style>body { color: #333 }</style>
  <script>window.dataLayer = ["ad-slot"];</script>
</head>
<body>
  <nav>首页 / 帮助</nav>
  <h1>退款政策</h1>
  <p>签收后 7 天内可申请退款。</p>
  <h3>申请条件</h3>
  <table>
    <thead><tr><th>项目</th><th>金额</th></tr></thead>
    <tbody><tr><td>运费</td><td>10</td></tr><tr><td>商品</td><td>100</td></tr></tbody>
  </table>
</body>
</html>
"""


def test_html_parser_emits_atx_headings_and_pipe_tables() -> None:
    """HTML 进、markdown 出：``#`` / ``###`` 与 ``|`` 管道表格，与 docx 那条路同形态。"""

    parsed = HtmlParser().parse(HTML_PAGE.encode(), filename="refund.html")

    assert "# 退款政策" in parsed.content
    assert "### 申请条件" in parsed.content
    assert "| 项目 | 金额 |" in parsed.content
    assert "| --- | --- |" in parsed.content
    assert "| 运费 | 10 |" in parsed.content
    assert "| 商品 | 100 |" in parsed.content
    assert "=====" not in parsed.content, "Setext 标题会让结构化切块器漏掉整篇章节"
    # head 是文档外壳：页面标题与脚本都不能混进正文切片。
    assert "退款政策 - 帮助中心" not in parsed.content
    assert "ad-slot" not in parsed.content
    assert parsed.source_type == "html"
    assert parsed.metadata == {"parser": "html"}


def test_html_parser_does_not_steal_markdown_or_text() -> None:
    registry = get_parser_registry()

    assert registry.parse("# 退款政策".encode(), filename="guide.md").source_type == "markdown"
    assert registry.parse("纯文本".encode(), filename="notes.txt").source_type == "text"
    assert registry.parse(HTML_PAGE.encode(), filename="page.html").source_type == "html"
    assert registry.parse(HTML_PAGE.encode(), filename="page.HTM").source_type == "html"


def test_html_parser_rejects_page_with_no_extractable_text() -> None:
    # head 里只剩标题、正文全空白：与 docx 一致，报「内容为空」而不是返回空串。
    page = b"<html><head><title>only a title</title></head><body> \n </body></html>"

    with pytest.raises(AppError) as exc_info:
        HtmlParser().parse(page, filename="blank.html")

    assert exc_info.value.code is ErrorCode.DOCUMENT_CONTENT_EMPTY


def test_html_parser_fails_decode_like_the_other_text_parsers() -> None:
    # 不把解码错误吞成 DOCUMENT_PARSE_FAILED：text / markdown 都是让
    # UnicodeDecodeError 冒泡，HTML 保持同一口径（编码问题要能被单独定位）。
    bad_bytes = b"<h1>\xff\xfe</h1>"

    with pytest.raises(UnicodeDecodeError):
        TextParser().parse(bad_bytes, filename="notes.txt")
    with pytest.raises(UnicodeDecodeError):
        HtmlParser().parse(bad_bytes, filename="page.html")


@dataclass
class _StubProvenance:
    page_number: int
    source: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class _StubPage:
    page_number: int
    markdown: str
    provenance: _StubProvenance


@dataclass
class _StubOcrResult:
    markdown: str
    pages: list[_StubPage]
    page_count: int
    pages_routed_to_ocr: list[int]
    pages_recommending_hosted: list[int]


def _fake_ocr_result(*_args: Any, **_kwargs: Any) -> _StubOcrResult:
    return _StubOcrResult(
        markdown="整篇拼装（本解析器不用它）",
        pages=[
            _StubPage(1, "# 第一章\n\n原生文本", _StubProvenance(1, "native")),
            _StubPage(2, "   ", _StubProvenance(2, "native", ["low confidence"])),
            _StubPage(3, "扫描页 OCR 文本", _StubProvenance(3, "ocr")),
        ],
        page_count=3,
        pages_routed_to_ocr=[3],
        pages_recommending_hosted=[2],
    )


def test_pdf_parser_joins_pages_with_page_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", _fake_ocr_result)

    parsed = PdfParser().parse(b"pdf-bytes", filename="manual.pdf")

    assert "## 第 1 页\n\n# 第一章" in parsed.content
    assert "## 第 3 页\n\n扫描页 OCR 文本" in parsed.content
    assert "## 第 2 页" not in parsed.content, "空白页不应产出页标记"
    assert parsed.source_type == "pdf"
    assert parsed.metadata["parser"] == "pdf"
    assert parsed.metadata["page_count"] == 3
    assert parsed.metadata["engine"] == ["native", "ocr"]
    assert parsed.metadata["ocr_pages"] == [3]
    assert parsed.metadata["pages_recommending_hosted"] == [2]
    assert parsed.metadata["warnings"] == ["low confidence"]


def test_pdf_parser_demotes_page_that_is_only_headings(monkeypatch: pytest.MonkeyPatch) -> None:
    """整页被 OCR 识别成标题时降级成正文，否则整篇文档会切成零个切片。

    实测：扫描页的 OCR 结果常是「## SCANNED PAGE」这种整页标题；直接拼上
    「## 第 N 页」后整页全是标题行，结构化切块器按低信息块丢弃，文档索引失败。
    """

    def _heading_only(*_args: Any, **_kwargs: Any) -> _StubOcrResult:
        return _StubOcrResult(
            markdown="## SCANNED PAGE",
            pages=[_StubPage(1, "## SCANNED PAGE", _StubProvenance(1, "ocr"))],
            page_count=1,
            pages_routed_to_ocr=[1],
            pages_recommending_hosted=[],
        )

    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", _heading_only)

    parsed = PdfParser().parse(b"pdf-bytes", filename="scan.pdf")

    assert "## 第 1 页\n\nSCANNED PAGE" in parsed.content
    assert "## SCANNED PAGE" not in parsed.content

    from app.utils.markdown_splitter import MarkdownStructuredSplitter

    pieces = MarkdownStructuredSplitter(800, 100, 10).split(parsed.content)
    assert pieces, "扫描页只有标题时也必须产出切片，不能让整篇索引失败"


def test_pdf_parser_rejects_result_with_no_extractable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _StubOcrResult(
        markdown="",
        pages=[
            _StubPage(1, " \n ", _StubProvenance(1, "native")),
            _StubPage(2, "", _StubProvenance(2, "ocr")),
        ],
        page_count=2,
        pages_routed_to_ocr=[2],
        pages_recommending_hosted=[1, 2],
    )
    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", lambda *_a, **_k: empty)

    with pytest.raises(AppError) as exc_info:
        PdfParser().parse(b"pdf-bytes", filename="scan.pdf")

    assert exc_info.value.code is ErrorCode.DOCUMENT_CONTENT_EMPTY


def test_pdf_parser_lets_deployment_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    # 缺 DLL / 模型获取失败 / OCR 执行错误在 Python 绑定里全是 ValueError：
    # 那是部署或执行问题，不能降级成 DOCUMENT_PARSE_FAILED 的 AppError。
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("failed to load PDFium")

    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", _boom)

    with pytest.raises(ValueError, match="failed to load PDFium"):
        PdfParser().parse(b"pdf-bytes", filename="manual.pdf")


def test_pdf_parser_publishes_native_runtime_paths_as_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 库只读环境变量，不认识 Settings 对象，所以解析前必须把配置写进 os.environ。
    monkeypatch.setenv("PDFIUM_LIB_PATH", "")
    monkeypatch.setenv("ORT_DYLIB_PATH", "")
    monkeypatch.setattr(settings, "PDFIUM_LIB_PATH", "C:/runtime/pdfium.dll")
    monkeypatch.setattr(settings, "ORT_DYLIB_PATH", "C:/runtime/onnxruntime.dll")
    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", _fake_ocr_result)

    PdfParser().parse(b"pdf-bytes", filename="manual.pdf")

    assert os.environ["PDFIUM_LIB_PATH"] == "C:/runtime/pdfium.dll"
    assert os.environ["ORT_DYLIB_PATH"] == "C:/runtime/onnxruntime.dll"


def test_registry_selects_parser_by_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    docx_result = _StubMarkupResult("<h1>退款政策</h1>")
    monkeypatch.setattr(mammoth, "convert_to_html", lambda _stream: docx_result)
    monkeypatch.setattr(pdf_inspector, "process_pdf_with_ocr_bytes", _fake_ocr_result)
    registry = get_parser_registry()

    assert registry.parse(b"# hello", filename="a.md").source_type == "markdown"
    assert registry.parse(b"plain", filename="b.txt").source_type == "text"
    assert registry.parse(b"docx-bytes", filename="c.docx").source_type == "docx"
    assert registry.parse(b"pdf-bytes", filename="d.pdf").source_type == "pdf"

    with pytest.raises(AppError) as exc_info:
        registry.parse(b"???", filename="e.xyz")
    assert exc_info.value.code is ErrorCode.DOCUMENT_PARSE_FAILED


def _document(**overrides: Any) -> Document:
    values: dict[str, Any] = {
        "id": "doc-1",
        "tenant_id": "tenant_demo",
        "kb_id": "kb-1",
        "title": "示例文档",
    }
    values.update(overrides)
    return Document(**values)


def test_prepare_uses_stored_content_and_never_reads_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(
        content="库内正文",
        source_type="text",
        source_file_path="C:/data/legacy.md",
        source_filename="legacy.md",
    )

    def _must_not_read(_path: Path) -> bytes:
        raise AssertionError("reparse=False 时不该读源文件")

    monkeypatch.setattr(Path, "read_bytes", _must_not_read)

    parsed = prepare_document_content(document)

    assert parsed.content == "库内正文"
    assert parsed.source_type == "text"
    assert parsed.metadata == {"from": "db"}


def test_prepare_with_reparse_reads_the_file_again(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document(
        content="旧正文",
        source_type="markdown",
        source_file_path="C:/data/doc.md",
        source_filename="doc.md",
    )
    monkeypatch.setattr(Path, "read_bytes", lambda _path: "# 新正文".encode())

    parsed = prepare_document_content(document, reparse=True)

    assert parsed.content == "# 新正文"
    assert parsed.source_type == "markdown"
    assert parsed.metadata["parser"] == "markdown"


def test_prepare_parses_source_file_when_content_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(
        content="",
        source_file_path="C:/data/notes.txt",
        source_filename="notes.txt",
    )
    monkeypatch.setattr(Path, "read_bytes", lambda _path: "文件正文".encode())

    parsed = prepare_document_content(document)

    assert parsed.content == "文件正文"
    assert parsed.source_type == "text"


def test_prepare_reports_missing_source_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_path: Path) -> bytes:
        raise FileNotFoundError("gone")

    monkeypatch.setattr(Path, "read_bytes", _missing)
    document = _document(source_file_path="C:/data/gone.pdf", source_filename="gone.pdf")

    with pytest.raises(AppError) as exc_info:
        prepare_document_content(document)

    assert exc_info.value.code is ErrorCode.DOCUMENT_PARSE_FAILED
    assert exc_info.value.msg == "source file is missing"


def test_prepare_rejects_parsed_file_with_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"   \n")
    document = _document(source_file_path="C:/data/blank.txt", source_filename="blank.txt")

    with pytest.raises(AppError) as exc_info:
        prepare_document_content(document)

    assert exc_info.value.code is ErrorCode.DOCUMENT_CONTENT_EMPTY


def test_prepare_rejects_document_with_neither_content_nor_file() -> None:
    document = _document(content="", source_file_path=None)

    with pytest.raises(AppError) as exc_info:
        prepare_document_content(document)

    assert exc_info.value.code is ErrorCode.DOCUMENT_PARSE_FAILED
    assert exc_info.value.msg == "document content and file are both empty"
