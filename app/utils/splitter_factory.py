"""按知识库配置选切块器。

只有一个分支点，但值得单独一层：写在 ``IndexingService.__init__`` 里会让
「策略从哪儿来」和「切块器怎么构造」两件事粘在一起，测试也不好转。

两种策略的差别：

* ``structure`` —— 认 Markdown 标题层级（h1/h2/h3 为边界），表格整表 + 行级片，
  代码块整块或递归切；
* ``size`` —— 纯字符窗口硬切，不认标题、不认表格、不认代码。

``size`` 刻意**不给原子块保护**：一旦它也开始认表格与代码，它和 ``structure``
的差别就只剩「认不认标题」，配置项也就失去意义。它存在的价值是给没有标题结构的
纯文本（日志、转录稿）一个稳定可预期的切法，同时充当评测里的朴素基线。
"""

from __future__ import annotations

from app.models.knowledge_base import (
    DEFAULT_CHUNK_SIZE,
    SPLIT_SIZE,
    SPLIT_STRUCTURE,
)
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.text_splitter import CharacterTextSplitter, TextSplitter


def build_splitter(
    strategy: str = SPLIT_STRUCTURE,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int | None = None,
    table_max_rows: int | None = None,
) -> TextSplitter:
    """按策略名构造切块器；未知策略回落到 ``structure``。

    回落而不是抛错：策略列有 CHECK 约束，正常路径不会出现非法值；真出现了
    （比如手工改库），带着默认策略继续索引比让整条链路失败更有用——切法不理想
    总好过文档永远进不了库。
    """

    from app.core.config import settings

    overlap = settings.CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    max_rows = (
        settings.TABLE_MAX_ROWS_PER_CHUNK if table_max_rows is None else table_max_rows
    )

    if strategy == SPLIT_SIZE:
        return CharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    return MarkdownStructuredSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        table_max_rows=max_rows,
    )
