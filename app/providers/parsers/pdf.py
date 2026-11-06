"""PDF 解析器：pdf-inspector 原生抽取 + 自动 OCR 路由，按页拼成 Markdown。

**失败二分（必须按这个口径）**：Python 绑定把所有 Rust 错误都拍平成
``ValueError``——缺 / 不兼容的 PDFium 或 ONNX Runtime、模型下载或校验失败、OCR
执行错误，全都只是同一类 ``ValueError``，没有专用异常类。因此这里**不捕获**
``ValueError``、更不解析它的文案把它归一成「文档解析失败」：它代表部署 / 执行
问题，应当冒泡到任务层记 ERROR、告警、不重试。

质量判断不是异常：低置信页或结果不完整时，库**成功返回** ``OcrPdfResult``，用
``pages_recommending_hosted`` / 每页 ``provenance.warnings`` 表达；写进 metadata
的 warnings 继续处理，只有整篇都抽不出文字才抛 ``DOCUMENT_CONTENT_EMPTY``。

所以唯一可靠的二分是「**拿到 ``OcrPdfResult`` 与否**」：拿到就成功，拿不到就是
执行问题。
"""

import os
import re
from typing import Literal, cast

import pdf_inspector

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.parsers.base import DocumentParser, ParsedDocument

# Settings 里 OCR_MODE 是普通 str（来自环境变量），库的类型存根要求字面量联合；
# 合法值由部署配置保证，这里只做类型收窄，不做运行期校验。
_OcrMode = Literal["off", "auto", "force"]

_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s+")


def _demote_heading_only_page(markdown: str) -> str:
    """整页只有标题行时，把 ``#`` 标记降级成正文。

    pdf-inspector 会把占据整页的大字识别成标题。若一页的 OCR 结果全都是标题行，
    结构化切块器会把它当「纯标题的低信息块」丢掉——于是一篇只有这种页的文档会切成
    零个切片、整篇索引失败，而页面上明明有字。这里把标题标记去掉，让文字落成正文，
    保证扫描页的内容仍能进索引。
    """

    lines = [line for line in markdown.splitlines() if line.strip()]
    if not lines:
        return markdown
    if any(not _HEADING_LINE_RE.match(line) for line in lines):
        return markdown
    return "\n".join(_HEADING_LINE_RE.sub("", line).strip() for line in lines)


def _publish_native_runtime_settings() -> None:
    """把 Settings 发布到库的 env-var 契约：库读环境变量，不读我们的配置对象。"""
    if settings.PDFIUM_LIB_PATH:
        os.environ["PDFIUM_LIB_PATH"] = settings.PDFIUM_LIB_PATH
    if settings.ORT_DYLIB_PATH:
        os.environ["ORT_DYLIB_PATH"] = settings.ORT_DYLIB_PATH


class PdfParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return filename.lower().endswith(".pdf") or mime == "application/pdf"

    def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
        _publish_native_runtime_settings()
        # 字节入参必须是 process_pdf_with_ocr_bytes；process_pdf_with_ocr 收的是路径。
        # 不 try/except：ValueError 就是部署/执行问题，见模块 docstring。
        result = pdf_inspector.process_pdf_with_ocr_bytes(
            file_bytes,
            mode=cast(_OcrMode, settings.OCR_MODE),
            offline=settings.OCR_OFFLINE,
            model_directory=settings.OCR_MODEL_DIRECTORY or None,
        )

        # 库没有「带页号输出」的开关，只能按 result.pages 自己拼；空白页整页丢弃。
        pages = [(page.page_number, page.markdown.strip()) for page in result.pages]
        pages = [(number, markdown) for number, markdown in pages if markdown]
        if not pages:
            raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "OCR 之后仍无可提取文字")

        content = "\n\n".join(
            f"## 第 {number} 页\n\n{_demote_heading_only_page(markdown)}"
            for number, markdown in pages
        )

        # 每页 provenance.warnings 拍平成字符串：metadata 要能直接 JSON 序列化。
        warnings = [str(warning) for page in result.pages for warning in page.provenance.warnings]
        return ParsedDocument(
            content=content,
            source_type="pdf",
            metadata={
                "parser": "pdf",
                "page_count": result.page_count,
                "engine": sorted({page.provenance.source for page in result.pages}),
                "ocr_pages": list(result.pages_routed_to_ocr),
                "pages_recommending_hosted": list(result.pages_recommending_hosted),
                "warnings": warnings,
            },
        )
