"""检索文本拼装：索引期那份「送进索引的文本」。

文档名 + 章节路径 + 正文，形如「文档：X 章节：Y 正文：Z」（见 GLOSSARY 的
「检索文本」）。它与**切片原文**不是一回事：切片原文（``chunks.content``）原样存库、
原样返回给调用方；检索文本只在索引期算出来，向量与（后续的）关键词索引共用同一份，
两路对比才有意义。

它是纯函数：不碰数据库、不 import 框架。空的段连同标签一起省略，所以没有章节路径时
绝不会留下「章节：」；三段全空时返回空串（空切片不该拿到一条只由标签组成的向量）。
每段首尾的空白会被去掉，段与段之间用一个空格连接。
"""

_DOCUMENT_LABEL = "文档："
_SECTION_LABEL = "章节："
_CONTENT_LABEL = "正文："


def build_retrieval_text(title: str, heading_path: str, content: str) -> str:
    """拼出检索文本：``文档：X 章节：Y 正文：Z``，空段不产生空标签。"""

    parts = [
        f"{label}{value.strip()}"
        for label, value in (
            (_DOCUMENT_LABEL, title),
            (_SECTION_LABEL, heading_path),
            (_CONTENT_LABEL, content),
        )
        if value.strip()
    ]
    return " ".join(parts)
