"""MarkdownStructuredSplitter：标题栈 / 表格整块 / 节内二次切 / 元数据契约。

全部离线：纯字符串输入，不连数据库、不发模型请求。
"""

from __future__ import annotations

from app.utils.markdown_splitter import MarkdownStructuredSplitter


def test_split_respects_heading_boundary() -> None:
    """一节内容不应被切到两个不同的 section。"""
    markdown = "# 退款政策\n退款时效为 7 个工作日。\n\n## 申请条件\n需提供订单号与支付凭证。\n"

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert [piece.metadata["heading_path"] for piece in pieces] == [
        "退款政策",
        "退款政策 > 申请条件",
    ]
    assert "退款时效" in pieces[0].text
    assert "退款时效" not in pieces[1].text, "上一节正文不能越过标题边界落到下一节"
    assert "需提供" not in pieces[0].text, "下一节正文不能越过标题边界落到上一节"


def test_section_id_is_deterministic() -> None:
    """同一输入两次 split 的 section_id 相同；不同标题路径的 section_id 不同。"""
    markdown = "# 账户安全\n修改密码需要二次验证。\n"
    splitter = MarkdownStructuredSplitter()

    first = splitter.split(markdown)
    second = splitter.split(markdown)
    other = splitter.split("# 支付方式\n支持扫码与转账。\n")

    assert [piece.metadata["section_id"] for piece in first] == [
        piece.metadata["section_id"] for piece in second
    ]
    assert first[0].metadata["section_id"] != other[0].metadata["section_id"]


def test_deeper_headings_do_not_start_new_sections() -> None:
    """### 只加深 heading_path，不触发 section flush。"""
    markdown = (
        "# 退款政策\n背景说明。\n\n### 时限要求\n七个工作日内到账。\n\n# 账户安全\n无关正文。\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert len(pieces) == 2
    assert pieces[0].metadata["heading_path"] == "退款政策 > 时限要求"
    assert "背景说明" in pieces[0].text
    assert "七个工作日内到账" in pieces[0].text, "### 的内容要留在父节里，不能单独成片"
    assert pieces[1].metadata["heading_path"] == "账户安全"


def test_table_kept_whole() -> None:
    """小表格整块保留，不跨切片。"""
    markdown = (
        "# 价格表\n"
        "\n"
        "| 套餐 | 价格 |\n"
        "| --- | --- |\n"
        "| 基础版 | 99 |\n"
        "| 专业版 | 199 |\n"
        "| 企业版 | 299 |\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    assert len(tables) == 1
    assert tables[0].metadata["table_part"] == 1
    assert tables[0].metadata["heading_path"] == "价格表"
    for row in ("| 基础版 | 99 |", "| 专业版 | 199 |", "| 企业版 | 299 |"):
        assert row in tables[0].text


def test_large_table_repeats_header() -> None:
    """超过 table_max_rows 的表按行分组，每组都重复表头。"""
    header = "| 编号 | 名称 |\n| --- | --- |"
    rows = "\n".join(f"| {number} | 商品{number} |" for number in range(1, 13))
    markdown = f"# 库存\n\n{header}\n{rows}\n"
    splitter = MarkdownStructuredSplitter(table_max_rows=5)

    pieces = splitter.split(markdown)

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    assert len(tables) == 3
    for part, piece in enumerate(tables, start=1):
        assert piece.metadata["table_part"] == part
        assert piece.text.startswith("| 编号 | 名称 |"), "每组第一行必须是表头"
        assert "| --- | --- |" in piece.text, "每组都要带分隔行"


def test_separator_line_is_dropped() -> None:
    """独立一行的 --- 跳过，不进入任何切片。"""
    markdown = "# 说明\n第一段。\n\n---\n\n第二段。\n"

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert len(pieces) == 1
    assert "第一段" in pieces[0].text
    assert "第二段" in pieces[0].text
    assert "---" not in pieces[0].text


def test_code_block_kept_whole() -> None:
    """代码块整块保留，即使超过 chunk_size 也不切。"""
    markdown = "# 示例\n\n```python\ndef add(a, b):\n    return a + b\n```\n"
    splitter = MarkdownStructuredSplitter(chunk_size=20, chunk_overlap=5)

    pieces = splitter.split(markdown)

    code_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert len(code_pieces) == 1
    assert "def add(a, b):" in code_pieces[0].text
    assert "return a + b" in code_pieces[0].text
    assert code_pieces[0].text.startswith("```")
    assert code_pieces[0].metadata["heading_path"] == "示例"


def test_low_information_pieces_filtered() -> None:
    """空正文、纯标题的 section 不产出切片。"""
    markdown = "# 只有标题\n\n## 也只有标题\n\n### 三级标题\n\n正文。\n"

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert len(pieces) == 1
    assert "正文" in pieces[0].text
    assert MarkdownStructuredSplitter().split("   \n\n \n") == [], "纯空白不应产出切片"


def test_duplicate_filter_is_scoped_to_immediately_preceding_piece() -> None:
    """重复内容只和同一节内紧邻的上一片比较，跨节的重复正文都要保留。"""
    paragraph = "同一段模板句。"
    table = "| 键 | 值 |\n| --- | --- |\n| a | 1 |"
    same_section_table = f"{table}\n\n{table}"
    markdown = (
        f"# 重复小节\n{paragraph}\n\n"
        f"# 重复小节\n{paragraph}\n\n"
        f"# 表格节\n\n{same_section_table}\n\n"
        f"# 后续表格节\n\n{table}\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    paragraphs = [piece for piece in pieces if piece.metadata["heading_path"] == "重复小节"]
    assert len(paragraphs) == 2, "两个不同 section 里的同一段模板句都必须保留"
    assert paragraphs[0].text == paragraphs[1].text

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    assert len(tables) == 2, "同一节内相邻重复表格折叠为一片，跨节的重复表格保留"
    assert [piece.metadata["heading_path"] for piece in tables] == ["表格节", "后续表格节"]
    assert all(piece.metadata["table_part"] == 1 for piece in tables)


def test_long_section_split_with_overlap() -> None:
    """超长节在节内二次切，带 overlap，且重叠不跨标题边界。"""
    body = "这是一段用于验证节内二次切的正文。" * 40
    markdown = f"# 第一节\n{body}\n\n## 第二节\n下一节的短正文。\n"
    splitter = MarkdownStructuredSplitter(chunk_size=120, chunk_overlap=30)

    pieces = splitter.split(markdown)

    first_section = [piece for piece in pieces if piece.metadata["heading_path"] == "第一节"]
    assert len(first_section) > 1, "超长节应被切成多片"
    assert all("第二节" not in piece.text for piece in first_section), (
        "为凑重叠不能把下一节标题拖进上一节"
    )
    for previous, current in zip(first_section, first_section[1:]):
        assert previous.text[-30:] in current.text, "相邻切片应保留 overlap"
    second_section = [
        piece for piece in pieces if piece.metadata["heading_path"] == "第一节 > 第二节"
    ]
    assert [piece.metadata["order_index"] for piece in second_section] == [0]
    assert "下一节的短正文" in second_section[0].text


def test_list_block_chunk_type_is_list() -> None:
    """以列表项为主的块标为 list。"""
    markdown = "# 步骤\n1. 准备材料\n2. 提交申请\n3. 等待审核\n"

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert len(pieces) == 1
    assert pieces[0].metadata["chunk_type"] == "list"


def test_no_heading_document_uses_empty_heading_path() -> None:
    """无标题文档整篇一节，heading_path 为空。"""
    pieces = MarkdownStructuredSplitter().split("第一段。\n\n第二段。\n")

    assert len(pieces) == 1
    assert pieces[0].metadata["heading_path"] == ""
    assert pieces[0].metadata["parent_section_id"] is None
    assert MarkdownStructuredSplitter().split("") == []


def test_metadata_contract_is_complete() -> None:
    """每片都带齐 6 个键；子节 parent_section_id 等于父节 section_id。"""
    markdown = "# 父章节\n父正文。\n\n## 子章节\n\n| 列 | 值 |\n| --- | --- |\n| a | 1 |\n"

    pieces = MarkdownStructuredSplitter().split(markdown)

    expected_keys = {
        "heading_path",
        "section_id",
        "parent_section_id",
        "order_index",
        "chunk_type",
        "table_part",
    }
    for piece in pieces:
        assert set(piece.metadata) == expected_keys
        assert len(str(piece.metadata["section_id"])) == 64
    parent = next(piece for piece in pieces if piece.metadata["heading_path"] == "父章节")
    child = next(piece for piece in pieces if piece.metadata["heading_path"] == "父章节 > 子章节")
    assert parent.metadata["parent_section_id"] is None
    assert child.metadata["parent_section_id"] == parent.metadata["section_id"]
    assert child.metadata["table_part"] == 1, "非分组表格也算第 1 部分"
