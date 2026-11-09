"""#64 · 用多语言代码语料钉住切块器的「代码块」行为。

为什么放测试 fixture 而不是评测语料：现有 127 题的基线靠 eval/corpus 复现，
往那里加文档会把基线数字改掉。代码切分该由单元行为保证，不该污染检索基线。

这份语料同时是 #66 的对照基准：换 Recursive 之后，这些断言应当继续成立。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.tokenizer import token_count

FIXTURE = Path(__file__).parent / "fixtures" / "markdown" / "code_blocks.md"


@pytest.fixture(scope="module")
def markdown() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# 用小窗口强制切分：语料里的 python 块约 511 token，默认 512 的窗口刚好装得下，
# 那样测不出「切开之后断点对不对」。这里的目的是验证切分行为，不是验证体感。
WINDOW = 200
OVERLAP = 20


@pytest.fixture(scope="module")
def pieces(markdown: str):
    return MarkdownStructuredSplitter(chunk_size=WINDOW, chunk_overlap=OVERLAP).split(markdown)


def test_long_python_block_is_split_into_parts(pieces) -> None:
    """超过窗口的 python 块必须被切开，并编号 code_part。

    这一条来自真实事故：代码块曾经整块产出，一片一万多字，检索拿它没辙。
    """

    code = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert code, "语料里的代码块应当被识别为 code 片"

    parts = [piece for piece in code if piece.metadata.get("code_part")]
    assert len(parts) >= 2, "长 python 块应当被切成多片并带 code_part 编号"


def test_every_piece_stays_within_the_window(pieces) -> None:
    """任何一片都不该超过窗口——包括代码块。"""

    limits = {"prose": WINDOW, "code": WINDOW, "table": WINDOW}
    for piece in pieces:
        kind = piece.metadata["chunk_type"]
        limit = limits.get(kind)
        if limit is None:
            continue
        assert token_count(piece.text) <= limit, (
            f"{kind} 片超过窗口：{token_count(piece.text)} token"
        )


def test_code_pieces_break_on_line_boundaries(markdown: str, pieces) -> None:
    """代码片必须从**原文的行首**开始，而不是把某一行劈成两半。

    判断方式：把原文按行切开后，每一片都应当能在「某个行偏移」处被完整匹配。
    以缩进开头不算违规——代码行本来就有缩进；违规的是从行的中间切。

    已知瑕疵：旧实现按「空行优先」断，后续片可能**以空行开头**（空行归到下一片）。
    换成 Recursive 之后这个瑕疵应当自然消失，届时可以把断言收紧。
    """

    code = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    continuation = [piece for piece in code if piece.metadata.get("code_part")]
    assert continuation, "需要至少一个二次切的代码片才能验证断点"

    offsets = [0]
    for line in markdown.splitlines(keepends=True)[:-1]:
        offsets.append(offsets[-1] + len(line))

    for piece in continuation:
        lines = piece.text.splitlines()
        assert lines, "代码片不能是空串"
        assert any(line.strip() for line in lines), "代码片不能是纯空白"
        assert any(markdown.startswith(piece.text, offset) for offset in offsets), (
            "代码片必须从原文的某个行首开始，不能从行中间切"
        )


def test_unfenced_language_still_gets_split(pieces) -> None:
    """没有语言标记的围栏（yaml / shell 输出）也要能切，不能整块留着。"""

    code_texts = [piece.text for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert any("services:" in text for text in code_texts), "无标记围栏应当被识别为 code 片"
    assert any("worker:" in text for text in code_texts)


def test_table_in_the_same_document_is_unaffected(pieces) -> None:
    """同一份文档里的表格仍要整表 + 行级片——切分改造不能波及表格。"""

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    rows = [piece for piece in pieces if piece.metadata["chunk_type"] == "table_row"]

    assert len(tables) == 1
    assert len(rows) == 4, "四个数据行各一片"


def test_heading_path_is_present_on_every_piece(pieces) -> None:
    """所有片都要带章节路径，代码片也不例外。"""

    assert all(piece.metadata.get("heading_path") for piece in pieces)
    assert any(piece.metadata["heading_path"].endswith("> 索引重建脚本") for piece in pieces)


@pytest.mark.parametrize("marker", ["perl", "python", "json", "yaml", "toml", "sql", ""])
def test_unknown_or_unimplemented_languages_do_not_crash(marker: str) -> None:
    """枚举里有但框架没实现的语言（实测 `perl`）不能把索引搞崩。

    这是踩过的坑：`Language.PERL` 是枚举成员，`from_language` 却抛
    `ValueError: Language Language.PERL is not implemented yet!`——整篇文档的
    索引会直接失败，而错误信息看起来像「文档坏了」。
    另外 `json` / `yaml` / `toml` / `sql` 根本不在枚举里，走通用分隔符。
    """

    from app.utils.window_splitter import code_pieces

    body = "def a():\n    return 1\n\ndef b():\n    return 2\n\ndef c():\n    return 3\n"
    pieces = code_pieces(body, marker=marker, size=8, overlap=1)

    assert pieces, f"{marker or '(无标记)'} 应当能切出片"
    assert all(piece.strip() for piece in pieces)
    assert pieces[0].lstrip().startswith("def a():"), "首片应当从代码开头开始"
