"""检索文本拼装的真值表。

检索文本是「送进索引与召回的那份文本」：文档名 + 标题路径 + 正文，格式为
``文档：X 章节：Y 正文：Z``（见 GLOSSARY 的「检索文本」）。它与切片原文不是一回事：
切片原文原样存库、原样返回，检索文本只在索引期算出来交给向量与关键词两路。

这里只断言纯函数的返回值，不碰数据库、不碰框架。边界由下表钉死：

* 空白的段（标题 / 章节路径 / 正文）不产生空标签，例如没有章节路径时绝不出现
  「章节：」；
* 章节路径与正文同时为空时只剩「文档：X」；
* 三段全空才返回空串——返回非空会让空切片也拿到一条向量。
"""

from __future__ import annotations

import pytest

from app.utils.retrieval_text import build_retrieval_text


@pytest.mark.parametrize(
    ("title", "heading_path", "content", "expected"),
    [
        (
            "XR-2000 安装手册",
            "安装 > 接线",
            "先把电源接上，再接通信号线。",
            "文档：XR-2000 安装手册 章节：安装 > 接线 正文：先把电源接上，再接通信号线。",
        ),
        # 无章节路径：不留空的「章节：」。
        (
            "XR-2000 安装手册",
            "",
            "先把电源接上。",
            "文档：XR-2000 安装手册 正文：先把电源接上。",
        ),
        # 无标题：不留空的「文档：」。
        (
            "",
            "安装 > 接线",
            "先把电源接上。",
            "章节：安装 > 接线 正文：先把电源接上。",
        ),
        # 多级章节路径原样保留，不重排、不截断。
        (
            "错误码查询",
            "错误码 > E1024 > 排查步骤",
            "先看日志里的 trace_id。",
            "文档：错误码查询 章节：错误码 > E1024 > 排查步骤 正文：先看日志里的 trace_id。",
        ),
        # 章节路径与正文同时为空：只剩文档名，不产出「章节： 正文：」这种空壳。
        (
            "XR-2000 安装手册",
            "",
            "",
            "文档：XR-2000 安装手册",
        ),
        # 三段全空：空串。否则空切片会拿到一条只由标签组成的向量。
        (
            "",
            "",
            "",
            "",
        ),
        # 只有空白的段等同空段。
        (
            "标题",
            "   \n  ",
            "  ",
            "文档：标题",
        ),
    ],
)
def test_build_retrieval_text_truth_table(
    title: str, heading_path: str, content: str, expected: str
) -> None:
    assert build_retrieval_text(title, heading_path, content) == expected


def test_build_retrieval_text_keeps_long_content_intact() -> None:
    """超长切片不截断、不改写：拼出来的尾部必须逐字等于正文。"""

    content = "错误码 E1024 表示证书过期。" * 400
    result = build_retrieval_text("XR-2000 安装手册", "错误码 > E1024", content)

    assert result.startswith("文档：XR-2000 安装手册 章节：错误码 > E1024 正文：")
    assert result.endswith(content)
    assert content in result
    assert len(result) == len("文档：XR-2000 安装手册 章节：错误码 > E1024 正文：") + len(content)
