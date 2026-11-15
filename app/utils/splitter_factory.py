"""按知识库配置选切块器。

只有一个分支点，但值得单独一层：写在 ``IndexingService.__init__`` 里会让
「策略从哪儿来」和「切块器怎么构造」两件事粘在一起，测试也不好转。

两种策略的差别：

* ``structure`` —— 认 Markdown 标题层级（h1/h2/h3 为边界），表格整表 + 行级片，
  代码块整块或递归切；
* ``size`` —— 固定 token 窗口，不认标题、不认表格、不认代码（片内按段落/句子回退）。

``size`` 刻意**不给原子块保护**：一旦它也开始认表格与代码，它和 ``structure``
的差别就只剩「认不认标题」，配置项也就失去意义。它存在的价值是给没有标题结构的
纯文本（日志、转录稿）一个稳定可预期的切法。**它不充当评测基线**——评测侧的
「朴素基线」是独立实现、只存在于 `scripts/eval_chunking.py`，两者分开演进
（见 GLOSSARY「朴素基线」与 ADR-0015 第三节）。
"""

from __future__ import annotations

from app.models.knowledge_base import (
    DEFAULT_CHUNK_SIZE,
    SPLIT_SIZE,
    SPLIT_STRUCTURE,
)
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.text_splitter import TextSplitter, WindowTextSplitter


def build_splitter(
    strategy: str = SPLIT_STRUCTURE,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int | None = None,
    table_max_rows: int | None = None,
    document_id: str = "",
) -> TextSplitter:
    """按策略名构造切块器；未知策略回落到 ``structure``。

    回落而不是抛错：策略列有 CHECK 约束，正常路径不会出现非法值；真出现了
    （比如手工改库），带着默认策略继续索引比让整条链路失败更有用——切法不理想
    总好过文档永远进不了库。

    ``document_id`` 进的是**节的身份证**（``stable_hash(文档 id : 标题路径)``，#143）：
    它必须由调用方给「我在切哪一份文档」，因为切块器自己只能看见正文。索引流水线永远
    传（有用例钉住这条接线）；评测 / 运维脚本只在自己进程里比较产物，可以不传——那时
    id 退化成「只在本篇内可比」。``size`` 策略不产 ``section_id``，这个参数对它无意义。
    """

    from app.core.config import settings

    overlap = settings.CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    max_rows = (
        settings.TABLE_MAX_ROWS_PER_CHUNK if table_max_rows is None else table_max_rows
    )

    if strategy == SPLIT_SIZE:
        return WindowTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    return MarkdownStructuredSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        table_max_rows=max_rows,
        document_id=document_id,
    )
