"""T6 OCR 契约：零路由不碰原生运行时、扫描件走 OCR、缺运行时抛 ValueError、全空页是质量错误。

**双层隔离**（见 `deploy/ocr/fetch_runtime.sh` 与 `.github/workflows/ci.yml` 的 ``ocr`` job）：
需要真实 PDFium / ONNX Runtime / PP-OCRv6 资产的三条用例同时挂 ``@pytest.mark.ocr``
与 ``skipif(not os.environ.get("PDF_INSPECTOR_MODEL_CACHE"))``，所以默认 ``pytest -q``
在没资产的机器上是 **skip** 而不是 fail，也不需要 ``addopts``（``addopts = -m "not ocr"``
会让 ``pytest -m ocr`` 叠加成空集）；OCR job 设好三个环境变量后 ``pytest -m ocr`` 才真跑。

**用例顺序有含义**：库在一个进程里只会成功加载一次原生运行时，一旦扫描件用例先跑通，
「路径指向不存在的库即抛 ``ValueError``」就会被掩盖。因此「零路由」与「缺运行时」两条
排在扫描件用例之前。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdf_inspector
import pytest

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.parsers.pdf import PdfParser

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"

# 测试资产由 `deploy/ocr/fetch_runtime.sh` 下载；没有资产时真跑 OCR 的用例整条跳过。
_requires_ocr_assets = pytest.mark.skipif(
    not os.environ.get("PDF_INSPECTOR_MODEL_CACHE"),
    reason="需要 PDFium / ONNX Runtime / PP-OCRv6 模型（见 deploy/ocr/fetch_runtime.sh）",
)


def _point_runtime_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """把两个原生库指到不存在的路径：能跑通就证明这条链路根本没碰它们。"""
    missing_pdfium = str(root / "missing" / "libpdfium.so")
    missing_ort = str(root / "missing" / "libonnxruntime.so")
    # Settings 是库的 env-var 契约来源，parse() 会把它们写回环境变量。
    monkeypatch.setattr(settings, "PDFIUM_LIB_PATH", missing_pdfium)
    monkeypatch.setattr(settings, "ORT_DYLIB_PATH", missing_ort)
    monkeypatch.setenv("PDFIUM_LIB_PATH", missing_pdfium)
    monkeypatch.setenv("ORT_DYLIB_PATH", missing_ort)


def _read_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


@pytest.mark.ocr
@_requires_ocr_assets
def test_native_text_pdf_needs_no_external_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 纯文字 PDF + mode="auto"：零路由页 → 库直接返回，不加载 PDFium / ORT、不读模型缓存、
    # 不联网。把两个库路径指向不存在的文件仍应成功——这就是「没碰原生运行时」的证据。
    monkeypatch.setattr(settings, "OCR_MODE", "auto")
    _point_runtime_at(monkeypatch, tmp_path)

    parsed = PdfParser().parse(_read_fixture("native_text.pdf"), filename="native.pdf")

    assert parsed.metadata["ocr_pages"] == []
    assert parsed.metadata["engine"] == ["native"]
    assert "Native text page" in parsed.content


@pytest.mark.ocr
@_requires_ocr_assets
def test_missing_native_runtime_is_value_error_not_business_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 部署问题 ≠ 质量判断：缺 PDFium 时上游只抛 ValueError，解析层不许把它吞成 AppError，
    # 否则任务层会把「镜像没配好」当成「这份文档质量差」而写 FAILED / 不告警。
    monkeypatch.setattr(settings, "OCR_MODE", "force")
    _point_runtime_at(monkeypatch, tmp_path)

    with pytest.raises(ValueError) as excinfo:
        PdfParser().parse(_read_fixture("native_text.pdf"), filename="native.pdf")

    assert not isinstance(excinfo.value, AppError)


@pytest.mark.ocr
@_requires_ocr_assets
def test_scanned_pdf_routes_to_ocr_and_yields_content(monkeypatch: pytest.MonkeyPatch) -> None:
    # 扫描件（只有图片、没有文字层）在 auto 模式下必须被路由到 OCR，且真能抽出文字。
    monkeypatch.setattr(settings, "OCR_MODE", "auto")
    # 资产预先灌在 PDF_INSPECTOR_MODEL_CACHE 里；offline=True 确保运行期不联网。
    monkeypatch.setattr(settings, "OCR_OFFLINE", True)

    parsed = PdfParser().parse(_read_fixture("scanned_page.pdf"), filename="scanned.pdf")

    assert "ocr" in parsed.metadata["engine"]
    assert parsed.content.strip()


@dataclass
class _EmptyOcrResult:
    """替身：编码层成功返回，但整篇一页文字都没有——这是质量判断，不是部署错误。"""

    page_count: int = 1
    pages: list[Any] = field(default_factory=list)
    pages_routed_to_ocr: list[int] = field(default_factory=list)
    pages_recommending_hosted: list[int] = field(default_factory=list)


def test_ocr_with_no_extracted_text_is_document_content_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 不需要任何资产：替换掉库入口即可（「拿到结果与否」是唯一二分线，拿到但全空走这里）。
    monkeypatch.setattr(
        pdf_inspector, "process_pdf_with_ocr_bytes", lambda *_args, **_kwargs: _EmptyOcrResult()
    )

    with pytest.raises(AppError) as excinfo:
        PdfParser().parse(b"%PDF-1.4\n", filename="blank.pdf")

    assert excinfo.value.code == ErrorCode.DOCUMENT_CONTENT_EMPTY
