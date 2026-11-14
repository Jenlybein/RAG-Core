"""#160：文档正文里的 tiktoken 特殊 token 字面量，不许让索引整篇失败。

**曾经的故障**：`docs/adr/0015` 的正文里含 `<|endoftext|>` 这个字面量，于是生产切块器抛
`ValueError: disallowed special token` ——**上传一篇正文里出现这个字面量的文档，整篇索引失败**。
技术文档讲提示词时提到它是正常内容，所以这是正常输入触发了内部错误。

三件事都钉住：

1. 计数不炸；2. 编解码往返不丢字面量；3. 切块器（真正的故障点）能切；
4. 结构守卫：三处编码调用都必须显式声明 `disallowed_special=()`，防止有人"顺手"删掉。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.utils.tokenizer import decode_ids, encode_ids, token_count
from app.utils.window_splitter import make_slicer

TOKENIZER = Path(__file__).resolve().parents[1] / "app" / "utils" / "tokenizer.py"

#: 三种字面量各来一次：这是**文档正文**，不是控制流。
LITERALS = "提示词用 <|endoftext|> 收尾，多轮用 <|im_start|> 与 <|im_end|> 包起来。"


def test_counting_a_document_that_mentions_a_special_token_does_not_raise() -> None:
    """正文提到特殊 token 是正常内容——它必须被当数据计数，而不是当控制信号拒绝。"""

    assert token_count(LITERALS) > 0
    assert token_count("") == 0


def test_the_literals_survive_an_id_roundtrip() -> None:
    """编解码往返必须逐字还原：正文不许因为编码被改写。"""

    ids = encode_ids(LITERALS)

    assert ids, "非空文本不该编码成空序列"
    assert decode_ids(ids) == LITERALS


def test_the_splitter_can_cut_such_a_document() -> None:
    """**真正的故障点在这里**：切块器用 token 尺子算窗口，它不能再抛。"""

    slicer = make_slicer()
    long_text = ("前文。" * 200) + LITERALS + ("后文。" * 200)

    pieces = slicer(long_text, 64, 8, "")

    assert pieces, "切块器必须产出片"
    # 不只是"没抛"：字面量必须**真的留在某个片里**，否则等于静默吞掉正文。
    assert any("<|endoftext|>" in piece for piece in pieces), (
        "正文里的字面量在切块时丢了——那比抛异常更难发现"
    )


def test_every_encode_call_declares_that_document_text_is_data() -> None:
    """结构守卫：`encode(` 的调用必须显式带 `disallowed_special=()`。

    为什么守这个而不是守行为：行为用例只能证明「这一条文本不炸」，而**新增一处 encode
    调用**的地方不会有人想起这条纪律——那是这个缺陷当初长出来的方式。
    """

    source = TOKENIZER.read_text(encoding="utf-8")
    calls = re.findall(r"\.encode\((?:[^()]|\([^()]*\))*\)", source)

    assert calls, "没扫到 encode 调用，说明守卫的正则已经跟不上源码了"
    missing = [call for call in calls if "disallowed_special=()" not in call]

    assert missing == [], (
        "这些 encode 调用没有声明 disallowed_special=()："
        f"{missing}——正文里的特殊 token 字面量会被当成控制流拒绝"
    )


@pytest.mark.parametrize("literal", ["<|endoftext|>", "<|im_start|>", "<|im_end|>"])
def test_each_registered_literal_is_usable_as_data(literal: str) -> None:
    """三个注册过的字面量逐个验：任何一个被漏掉，那一篇文档就传不上去。"""

    text = f"左 {literal} 右"

    assert token_count(text) > 0
    assert decode_ids(encode_ids(text)) == text


def test_the_production_markdown_splitter_indexes_a_document_with_a_special_literal() -> None:
    """**生产索引链路**：结构化切块器（`MarkdownStructuredSplitter`）能吃下这篇文档。

    #160 的验收标准要求「构造一篇正文含字面量的文档，走生产索引链路」——只测 `make_slicer()`
    不够：真正炸掉索引的是这个类（标题栈 → 分块 → 每块用 token 尺子算窗口）。
    """

    from app.utils.markdown_splitter import MarkdownStructuredSplitter

    document = (
        "# 提示词约定\n"
        "\n"
        "## 定界符\n"
        "\n"
        f"正文里的定界符写作 {LITERALS}\n"
        "\n"
        "## 多轮对话\n"
        "\n"
        "多轮消息用 <|im_start|> 与 <|im_end|> 包起来，收尾用 <|endoftext|>。\n"
    )

    chunks = MarkdownStructuredSplitter(chunk_size=64, chunk_overlap=8).split(document)

    assert chunks, "生产切块器必须产出切片"
    joined = "".join(piece.text for piece in chunks)
    assert "<|endoftext|>" in joined, "字面量在结构化切块时丢了——那比抛异常更难发现"
    assert all(piece.metadata.get("heading_path") for piece in chunks), "标题栈仍要成立"
