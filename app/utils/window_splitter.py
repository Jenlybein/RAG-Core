"""窗口切分：按分隔符优先级递归回退，长度按 Qwen token 计。

**为什么不再自写断点搜索**：旧实现是「开一个字符窗口，超了就往回找一个 `\\n`」。
它只认换行，中文句子（以 `。` 结尾）不在判断里；代码块另有一套「空行 → 缩进 → 换行」
的手工规则。两套逻辑都在重复「在哪断最不破坏语义」这件事，而框架已经把它做成
分隔符优先级。

**框架不管的部分仍归我们**：标题栈、章节路径、表格整表与行级片、页码。这些是折叠
（ADR-0013）的前提，`RecursiveCharacterTextSplitter` 一概不产出，所以它只接管
「一片之内怎么再切」。

**代码块按语言切**：先解析围栏的语言标记，能映射到 `Language` 枚举就按语法切，
否则（yaml / toml / shell / 无标记）回退通用分隔符。这里不做手工白名单——枚举里
有就用，没有就退，维护成本为零。

**这里没有「硬切」实现。** 评测要做「算法 × 单位」的交叉对照时，基线的硬切由
`scripts/eval_chunking.py` 自带（见 ADR-0015 §三与词表「朴素基线」）——评测专用的
切法不进生产，否则基线就不再独立，两边的演进会互相牵制。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from app.utils.tokenizer import token_count

# 长度尺子：生产是 Qwen token；对照实验里可能是字符（`len`）。
LengthFunction = Callable[[str], int]

# 一个切片器的完整签名：文本 → 片列表。
# `marker` 是围栏上的语言标记，空串表示「这段不是代码」。
Slicer = Callable[[str, int, int, str], list[str]]

# 正文分隔符。**中文标点必须在列表里**：`RecursiveCharacterTextSplitter` 的官方
# 示例给的是 `". "`（英文句号 + 空格），只覆盖英文；中文没有空格，会一路塌到
# 最后那层按字硬切。所以中英各给一套，顺序即优先级：
#
#   段落 → 换行 → 句末标点（中英）→ 分句标点 → 兜底按字
#
# 最后那层 `""` 只在「整段没有任何标点」时才生效——没有它，一段无标点的长文本
# 会整块超窗口。
TEXT_SEPARATORS: Final[tuple[str, ...]] = (
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    ". ",
    "! ",
    "? ",
    "；",
    "; ",
    "",
)

# 代码块：只按行切（空行优先，其次任意换行）。
#
# **刻意不给 `" "` 与 `""`**：那两层会把缩进当分隔符吃掉（`            timeout=60,`
# 被切成 `timeout=60,`），代码片丢失缩进就不可读了。代价是「单行超过窗口」时整行
# 留在片里（超窗口）——这比把语法切碎更好，且真实的超长单行极罕见。
CODE_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n")


def language_of(marker: str) -> Language | None:
    """把围栏上的语言标记映射到 `Language`；不认识就返回 `None`。

    `Language` 是字符串枚举，所以直接按值构造即可——**不做手工白名单**，
    枚举里新增语言不需要改这里。
    """

    marker = (marker or "").strip().lower()
    if not marker:
        return None
    try:
        return Language(marker)
    except ValueError:
        return None


def _trim(piece: str) -> str:
    """只剥首尾换行，**不碰缩进与行内空白**。

    `keep_separator` 会把分隔的换行留在片首，剥掉它片才是从行首开始；而代码的缩进
    是语义，绝不能被 strip。行尾的空白同样不该动——它可能是代码对齐的一部分。
    """

    return piece.strip("\n")


def _build(
    *,
    size: int,
    overlap: int,
    separators: tuple[str, ...],
    marker: str,
    length_function: LengthFunction,
) -> RecursiveCharacterTextSplitter:
    """构造切分器。

    `strip_whitespace=False` 是**代码块的必需项**：框架默认会把每片两端的空白削掉，
    而代码的缩进就是语义——`timeout=60,` 与 `            timeout=60,` 不是同一行。
    正文也用同一个值：两端多出的换行由 `_trim` 收掉，空格本身有意义。

    分隔符归属也按有无语言分开：
    - **无语言**用 `keep_separator="end"`，把「。」「\n\n」留在前一片末尾，
      否则下一片会以标点开头（实测英文片以 `. Delta` 起始）。
    - **有语言**保持框架默认：`from_language` 的分隔符里含 `"\\ndef "` 这类
      关键字，归到前一片会把 `def` 切走，下一片只剩 `b():`——语法就断了。
    """

    language = language_of(marker)
    if language is not None:
        try:
            return RecursiveCharacterTextSplitter.from_language(
                language=language,
                chunk_size=size,
                chunk_overlap=overlap,
                length_function=length_function,
                strip_whitespace=False,
            )
        except (NotImplementedError, ValueError):
            # `Language` 枚举里有、但框架没实现分隔符表的成员。实测 `perl` 抛的是
            # `ValueError: Language Language.PERL is not implemented yet!`（不是
            # NotImplementedError），两种都要接住——它不该让整篇索引崩掉。
            pass
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        length_function=length_function,
        strip_whitespace=False,
        keep_separator="end",
        separators=list(separators),
    )


def make_slicer(length_function: LengthFunction = token_count) -> Slicer:
    """产出一个切片器：窗口按 `length_function` 计，按分隔符回退。

    参数化的唯一目的是对照实验：同一套逻辑分别用字符尺子与 token 尺子跑一遍，
    才能把「换单位」的影响单独拆出来。生产固定用默认的 token 尺子。
    """

    def slicer(text: str, size: int, overlap: int, marker: str) -> list[str]:
        stripped = text.strip("\n")
        if not stripped:
            return []
        if length_function(stripped) <= size:
            return [stripped]
        separators = CODE_SEPARATORS if marker else TEXT_SEPARATORS
        splitter = _build(
            size=size,
            overlap=overlap,
            separators=separators,
            marker=marker,
            length_function=length_function,
        )
        return [
            _trim(piece) for piece in splitter.split_text(stripped) if piece.strip()
        ]

    return slicer


# 生产默认：Qwen token 计长 + 分隔符回退。
default_slicer: Final[Slicer] = make_slicer()


def text_pieces(text: str, *, size: int, overlap: int) -> list[str]:
    """把一段正文切成不超过 `size` 个 token 的片；空文本返回空列表。"""

    return default_slicer(text, size, overlap, "")


def code_pieces(text: str, *, marker: str, size: int, overlap: int) -> list[str]:
    """把一段代码切成不超过 `size` 个 token 的片；按围栏语言选择分隔符。

    生产路径由 ``markdown_splitter`` 经 ``default_slicer`` 调用，测试也会直接
    import 它来钉「代码块必须按行边界断开」这条性质——两者缺一不可。
    """

    return default_slicer(text, size, overlap, marker)
