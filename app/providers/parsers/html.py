"""HTML 解析器：BeautifulSoup 取正文 → markdownify 转 Markdown。

**为什么不是 markitdown**：它的 html 路线本来就是 bs4 + markdownify 子类，两者都已在
依赖里；而 markitdown 的基座安装会拖进 magika + onnxruntime（原生运行时，几百 MB）
这套只用于文件类型嗅探的东西，对「把一个 html 串转成文本」毫无用处。这里直接用
同一对库：产出形态一致，不新增运行时负担。

``heading_style="ATX"`` 与 docx 那条路同因：markdownify 默认输出 Setext
（``标题`` 下一行 ``======``），而结构化切块器只认 ``#`` / ``##``，用默认值会让每一级
标题退化成正文。表格不用自己拼——markdownify 自带 ``convert_table``，输出就是
``| a | b |`` 加 ``| --- | --- |`` 的管道表格。

只取 ``<body>``：``<head>`` 里是 title / meta / style，属于文档外壳而不是正文，
整篇转会把页面标题塞成第一段正文，白白污染切片。
"""

from bs4 import BeautifulSoup
from markdownify import markdownify

from app.core.exceptions import AppError, ErrorCode
from app.providers.parsers.base import DocumentParser, ParsedDocument


class HtmlParser(DocumentParser):
    def supports(self, filename: str, mime: str = "") -> bool:
        return filename.lower().endswith((".html", ".htm")) or mime in (
            "text/html",
            "application/xhtml+xml",
        )

    def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
        # 与 text / markdown 解析器同一口径：严格 UTF-8 解码，解码失败不吞成
        # 「解析失败」——那是输入编码问题，冒泡出去比换一个错误码更好定位。
        text = file_bytes.decode("utf-8")
        soup = BeautifulSoup(text, "html.parser")
        root = soup.body or soup

        content = markdownify(str(root), heading_style="ATX")
        if not content.strip():
            raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "html 中没有可提取文字")

        return ParsedDocument(
            content=content,
            source_type="html",
            metadata={"parser": "html"},
        )
