"""生成 PDF 解析用的多页 fixture（带标题层级、一张表格、三页）。

为什么需要它：`native_text.pdf` 只有 1 页 1 行、没有标题也没有表格，验证不了
「PDF → markdown 的结构还原」。这个 fixture 用真实排版（字号区分层级、表格画线）
让 pdf-inspector 有机会识别出结构，从而把四件事定住：

1. 标题层级 → `heading_path`（h1 > h2）；
2. 表格 → markdown 表格（`|` 语法，行级表片的前提）；
3. 多页 → `## 第 N 页` 与 `page` 字段；
4. 端到端：解析 → 切块（片数、类型、路径、页号）。

**必须用嵌入的 TrueType 中文字体，不能用 reportlab 的 `STSong-Light`**：后者是
CJK CID 预定义字体，没有 ToUnicode 映射，原生抽取拿不到任何字符——实测表现为
`OCR_MODE=off` 直接抛 `DOCUMENT_CONTENT_EMPTY`，`auto` 则把整篇判定为「需要 OCR」
而去加载 PDFium。那样测到的永远是 OCR 路径，反而验证不了原生结构还原。

它是**原生文字层** PDF（不是扫描件），所以不需要 PDFium / ONNX Runtime / 模型。

重新生成：

    uv run --with reportlab python scripts/make_pdf_fixture.py
"""

from __future__ import annotations

import pathlib

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = pathlib.Path(__file__).resolve().parent.parent / "tests/fixtures/ocr/structured_pages.pdf"

# 候选字体：(路径, ttc 子字体序号)。取第一个存在的，保证生成的 PDF 带 ToUnicode。
FONT_CANDIDATES: tuple[tuple[str, int | None], ...] = (
    ("C:/Windows/Fonts/simhei.ttf", None),
    ("C:/Windows/Fonts/Deng.ttf", None),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
)


def _register_font() -> str:
    for path, index in FONT_CANDIDATES:
        if not pathlib.Path(path).exists():
            continue
        kwargs = {} if index is None else {"subfontIndex": index}
        pdfmetrics.registerFont(TTFont("CJK", path, **kwargs))
        return "CJK"
    raise SystemExit(
        "找不到可用的中文 TrueType 字体；请在 FONT_CANDIDATES 里补一个路径。"
        "不要退回 STSong-Light——它没有 ToUnicode，抽不出文字。"
    )


FONT = _register_font()

H1 = ParagraphStyle("h1", fontName=FONT, fontSize=22, leading=28, spaceAfter=10)
H2 = ParagraphStyle("h2", fontName=FONT, fontSize=15, leading=20, spaceAfter=8)
BODY = ParagraphStyle("body", fontName=FONT, fontSize=10, leading=16, spaceAfter=8)


def _table() -> Table:
    rows = [
        ["型号", "千兆网口", "万兆光口", "转发吞吐"],
        ["XR-2000", "4", "0", "900 Mbps"],
        ["XR-2100", "8", "2", "2.4 Gbps"],
        ["XR-2000 Pro", "6", "0", "1.5 Gbps"],
    ]
    table = Table(rows, colWidths=[110, 80, 80, 100])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return table


def build() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, title="部署实施指南")
    story = [
        Paragraph("部署实施指南", H1),
        Paragraph("本指南说明设备上架、供电接入与初始配置的完整流程。", BODY),
        Paragraph("上架与供电", H2),
        Paragraph(
            "机架款必须使用导轨固定，前后各留 10 厘米散热空间。供电需双路冗余，"
            "任一回路失电不影响业务。",
            BODY,
        ),
        PageBreak(),
        Paragraph("整机规格", H2),
        Paragraph("各型号的接口与转发能力如下表。", BODY),
        _table(),
        Spacer(1, 10),
        Paragraph("表中吞吐为开启基本功能后的实测值，开启 VPN 后按比例衰减。", BODY),
        PageBreak(),
        Paragraph("常见问题", H2),
        Paragraph("上电后指示灯不亮，先检查电源适配器是否与机型匹配。", BODY),
        Paragraph("更换适配器后仍不亮，联系售后并提供设备序列号。", BODY),
    ]
    doc.build(story)
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节，字体 {FONT}）")


if __name__ == "__main__":
    build()
