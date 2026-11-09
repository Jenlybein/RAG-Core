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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Literal

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from app.utils.tokenizer import decode_ids, encode_ids, token_count

# 窗口算法：`recursive` 按分隔符回退（生产默认）；`hard` 按固定窗口硬切，
# 只在评测的交叉对照里当基线用——它不知道句子和行，正是要对照的那一侧。
WindowMode = Literal["hard", "recursive"]
LengthFunction = Callable[[str], int]

# 正文分隔符。**中文标点必须在列表里**：`RecursiveCharacterTextSplitter` 的官方
# 示例给的是 `". "`（英文句号 + 空格），中文里几乎不命中，会一路塌到 `""` 按字硬切。
# 顺序即优先级：先按段落断，退而按换行，退而按句末标点，最后兜底按字。
TEXT_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n", "。", "！", "？", "；", "，", "")

# 无语言标记的代码块：只按行切（空行优先，其次任意换行）。
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


def _hard_char_slice(text: str, *, size: int, overlap: int) -> list[str]:
    """按字符固定窗口硬切，不做任何回退。"""

    step = size - overlap
    pieces: list[str] = []
    start = 0
    while start < len(text):
        pieces.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += step
    return pieces


def _hard_token_slice(text: str, *, size: int, overlap: int) -> list[str]:
    """按 token 固定窗口硬切：先编码、按 token 索引取窗、再解码。"""

    ids = encode_ids(text)
    step = size - overlap
    pieces: list[str] = []
    start = 0
    while start < len(ids):
        pieces.append(decode_ids(ids[start : start + size]))
        if start + size >= len(ids):
            break
        start += step
    return pieces


def _hard_slice(
    text: str, *, size: int, overlap: int, length_function: LengthFunction
) -> list[str]:
    """硬切：按传入的**尺子**决定粒度。

    尺子是字符（`len`）就切字符，是 token 就切 token——这正是对照实验要分开的
    那个变量。
    """

    if length_function(text) == len(text):
        return _hard_char_slice(text, size=size, overlap=overlap)
    return _hard_token_slice(text, size=size, overlap=overlap)


def _trim(piece: str) -> str:
    """只剥首尾换行，**不碰缩进与行内空白**。

    `keep_separator` 会把分隔的换行留在片首，剥掉它片才是从行首开始；而代码的缩进
    是语义，绝不能被 strip。
    """

    return piece.strip("\n")


def _build(
    *,
    size: int,
    overlap: int,
    separators: tuple[str, ...],
    language: Language | None,
    length_function: LengthFunction = token_count,
) -> RecursiveCharacterTextSplitter:
    """构造切分器。

    `strip_whitespace=False` 是**代码块的必需项**：框架默认会把每片两端的空白削掉，
    而代码的缩进就是语义——`timeout=60,` 与 `            timeout=60,` 不是同一行。
    正文也用同一个值：正文两端多出的空白本身没有意义，由调用方 strip。
    """

    if language is not None:
        return RecursiveCharacterTextSplitter.from_language(
            language=language,
            chunk_size=size,
            chunk_overlap=overlap,
            length_function=length_function,
            strip_whitespace=False,
        )
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        length_function=length_function,
        strip_whitespace=False,
        separators=list(separators),
    )


def text_pieces(
    text: str,
    *,
    size: int,
    overlap: int,
    length_function: LengthFunction = token_count,
    mode: WindowMode = "recursive",
) -> list[str]:
    """把一段正文切成不超过 `size` 个 token 的片；空文本返回空列表。"""

    stripped = text.strip("\n")
    if not stripped:
        return []
    if length_function(stripped) <= size:
        return [stripped]
    if mode == "hard":
        return [_trim(piece) for piece in _hard_slice(
            stripped, size=size, overlap=overlap, length_function=length_function
        ) if piece.strip()]
    splitter = _build(
        size=size,
        overlap=overlap,
        separators=TEXT_SEPARATORS,
        language=None,
        length_function=length_function,
    )
    return [_trim(piece) for piece in splitter.split_text(stripped) if piece.strip()]


def code_pieces(
    text: str,
    *,
    marker: str,
    size: int,
    overlap: int,
    length_function: LengthFunction = token_count,
    mode: WindowMode = "recursive",
) -> list[str]:
    """把一段代码切成不超过 `size` 个 token 的片；按围栏语言选择分隔符。"""

    stripped = text.strip("\n")
    if not stripped:
        return []
    if length_function(stripped) <= size:
        return [stripped]
    if mode == "hard":
        return [_trim(piece) for piece in _hard_slice(
            stripped, size=size, overlap=overlap, length_function=length_function
        ) if piece.strip()]
    splitter = _build(
        size=size,
        overlap=overlap,
        separators=CODE_SEPARATORS,
        language=language_of(marker),
        length_function=length_function,
    )
    return [_trim(piece) for piece in splitter.split_text(stripped) if piece.strip()]
