"""#172 · 正文的规范化与校验和：判重口径的**唯一**入口（ADR-0005 D8）。

判重（#173 之后的建版本）、版本行、迁移回填共用同一个函数，所以这里钉的是这个函数本身
的口径，而不是某个调用点：同一个意思的两种写法必须算出同一个校验和，不同内容必须算出
不同的校验和。

两条要守的东西：

1. **规范化只磨三样**：换行统一成 ``\\n``、Unicode 归到 NFC、去掉首尾空白。别的字符
   （中间的空格、缩进、大小写、全半角）一律**原样计分**——校验和要是「顺手多磨了一刀」，
   判重就会把两次真改动判成「内容没变」，而那是静默丢一次改稿；
2. **校验和的期望值来自独立来源**：下面这些 64 位十六进制是拿 .NET 的 SHA256 对
   **手写的字面量**算出来的，不是用被测函数再算一遍（那样断言只能证明「函数等于它自己」）。
"""

from __future__ import annotations

import pytest

from app.utils.checksum import CHECKSUM_HEX_LENGTH, checksum_of, normalize_text

#: 手写字面量的 sha256（.NET SHA256 / UTF-8），故意不经过本模块算出来。
SHA256_OF_REFUND_SENTENCE = "24a0752703eb616c007ab44fcc4241c42e0ecdc0409a84767f9279da79807cd2"
#: ``"é"`` 的 NFC 单码位形态（U+00E9）的 sha256，同上。
SHA256_OF_LATIN_SMALL_E_ACUTE = "4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c"
#: 空正文的 sha256——空正文是合法正文（没有切片的失败文档），必须有一个稳定的值。
SHA256_OF_EMPTY_TEXT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

REFUND_SENTENCE = "退款流程见附件。"


# ────────────────────────────────────────────────────── 规范化：只磨该磨的那三样


def test_crlf_and_cr_become_lf() -> None:
    """同一个 Windows 文件与 Unix 文件的换行不该算出两个校验和。"""

    assert normalize_text("第一行\r\n第二行\r第三行") == "第一行\n第二行\n第三行"


def test_leading_and_trailing_whitespace_is_dropped() -> None:
    assert normalize_text("\n\n  退款流程见附件。\t\n ") == REFUND_SENTENCE


def test_unicode_is_folded_to_nfc() -> None:
    """``e`` + 组合重音（NFD）与单个 ``é``（NFC）是同一个字符。"""

    decomposed = "e\u0301"

    assert normalize_text(decomposed) == "\u00e9"
    assert len(normalize_text(decomposed)) == 1, "NFD 两码位必须折成一个"


def test_inner_whitespace_and_case_are_left_alone() -> None:
    """只磨首尾与换行：中间的空格、缩进、大小写都算内容。"""

    assert (
        normalize_text("  第一段\n\n    缩进的两行  \n第二段  ")
        == "第一段\n\n    缩进的两行  \n第二段"
    )


def test_empty_and_blank_text_normalize_to_empty_string() -> None:
    assert normalize_text("") == ""
    assert normalize_text(" \r\n\t\n ") == ""


# ───────────────────────────────────────────────────────────── 校验和：唯一入口


def test_checksum_is_a_64_hex_sha256() -> None:
    digest = checksum_of(REFUND_SENTENCE)

    assert len(digest) == CHECKSUM_HEX_LENGTH == 64
    assert digest == SHA256_OF_REFUND_SENTENCE


def test_the_version_row_column_is_wide_enough_for_the_digest() -> None:
    """版本行的 ``checksum`` 列装得下摘要本身：宽度与摘要长度**同一来源**。

    这两个数字曾经各写一份（模型的 ``CHECKSUM_LENGTH`` 与 :mod:`app.utils.checksum` 的
    ``CHECKSUM_HEX_LENGTH``），改口径时只会改到一边——症状是写入被截断、或迁移与模型的列宽
    不一致，而两者都不报错。断言取「摘要真的有多长」，不是再写一个 64。
    """

    from app.models.document_version import DocumentVersion

    column = DocumentVersion.__table__.c.checksum
    assert column.type.length == len(checksum_of(REFUND_SENTENCE))


def test_the_same_content_written_differently_yields_one_checksum() -> None:
    """同一份正文的三种写法（Windows 换行 / 首尾空白 / 两者叠加）只有一个校验和。"""

    assert checksum_of(REFUND_SENTENCE + "\r\n") == SHA256_OF_REFUND_SENTENCE
    assert checksum_of("\n  " + REFUND_SENTENCE + "  \t") == SHA256_OF_REFUND_SENTENCE
    assert checksum_of("\r\n " + REFUND_SENTENCE + "\r\n") == SHA256_OF_REFUND_SENTENCE


def test_composed_and_decomposed_accents_hash_the_same() -> None:
    """同一条正文从两个输入法落盘（NFC / NFD）不该被当成「改过一稿」。"""

    assert checksum_of("e\u0301") == checksum_of("\u00e9") == SHA256_OF_LATIN_SMALL_E_ACUTE


def test_empty_content_still_has_a_checksum() -> None:
    assert checksum_of("") == checksum_of("  \r\n\t ") == SHA256_OF_EMPTY_TEXT


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("退款流程见附件。", "退款流程见附件！"),
        ("退款流程见附件。", "退款流程见 附件。"),
        ("第一段\n\n第二段", "第一段\n第二段"),
        ("Abc", "abc"),
    ],
)
def test_any_real_edit_changes_the_checksum(left: str, right: str) -> None:
    """判重的另一半：真改了就必须看出来（换行**位置**变了也算改动）。"""

    assert checksum_of(left) != checksum_of(right)
