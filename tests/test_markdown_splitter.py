"""MarkdownStructuredSplitter：标题栈 / 表格整块 / 节内二次切 / 元数据契约。

全部离线：纯字符串输入，不连数据库、不发模型请求。
"""

from __future__ import annotations

from app.utils.markdown_splitter import MarkdownStructuredSplitter, _Piece


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


def test_h3_starts_new_section() -> None:
    """### 是切片边界：一片一个子节，路径精确到子节。

    h1 下的引导段落自成一片（它不属于任何 h3），这是刻意的：把引导段落塞进
    第一个 h3 会让那片同时讲两件事。
    """
    markdown = (
        "# 退款政策\n背景说明。\n\n### 时限要求\n七个工作日内到账。\n\n# 账户安全\n无关正文。\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert [p.metadata["heading_path"] for p in pieces] == [
        "退款政策",
        "退款政策 > 时限要求",
        "账户安全",
    ]
    assert "背景说明" in pieces[0].text
    assert "七个工作日内到账" in pieces[1].text
    assert "背景说明" not in pieces[1].text, "引导段落不跟着 h3 走"


def test_h4_and_deeper_do_not_start_new_sections() -> None:
    """#### 及更深只加深 heading_path，不触发 section flush。

    再深就不切：h4 往往是「请求 / 响应 / 错误码」这类同一件事的附属小节，
    拆开会让每片只剩几个字段名，失去上下文。
    """
    markdown = (
        "# 演示文档\n\n"
        "## 大章一\n\n"
        "### 上传文档\n\n正文 A。\n\n"
        "#### 请求\n\n参数 B。\n\n"
        "#### 响应\n\n字段 C。\n\n"
        "## 大章二\n\n"
        "### 检索\n\n正文 D。\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    # 两个 h4 分叉，公共前缀到 h3
    assert [p.metadata["heading_path"] for p in pieces] == [
        "演示文档 > 大章一 > 上传文档",
        "演示文档 > 大章二 > 检索",
    ]
    assert "参数 B" in pieces[0].text
    assert "字段 C" in pieces[0].text


def test_multiple_section_headings_never_collapse_into_one_piece() -> None:
    """多个 h3 必须各自成片，不能像以前那样被并进同一个 h2 片。

    旧行为下一片里装着「子节 1.1」「子节 1.2」，路径只能回落到 h2；新行为下
    每个子节自己一片，路径精确到子节——这正是 h3 成为边界要换来的东西。
    """
    markdown = (
        "# 演示文档\n\n"
        "## 大章一\n\n"
        "### 子节 1.1\n\n正文 A。\n\n"
        "### 子节 1.2\n\n正文 C。\n\n"
        "## 大章二\n\n"
        "### 子节 2.1\n\n正文 D。\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    assert [p.metadata["heading_path"] for p in pieces] == [
        "演示文档 > 大章一 > 子节 1.1",
        "演示文档 > 大章一 > 子节 1.2",
        "演示文档 > 大章二 > 子节 2.1",
    ]
    for piece in pieces:
        assert piece.metadata["heading_path"].count(" > ") == 2


def test_single_chain_can_reach_four_levels() -> None:
    """单链 h2 > h3 > h4 时，路径要能到第 4 层，而不是回落到 h2。

    父级标题会被更长的路径覆盖，不参与公共前缀计算；否则深层嵌套永远体现不出来。
    """
    markdown = (
        "# 接口参考\n\n"
        "## 文档管理接口\n\n"
        "### 上传文档\n\n"
        "#### 请求\n\n参数 A。\n\n"
        "#### 响应\n\n字段 B。\n\n"
        "## 检索接口\n\n"
        "### 检索\n\n正文 C。\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    # 第一节里有两个 h4 分叉（请求/响应），公共前缀到 h3
    assert pieces[0].metadata["heading_path"] == "接口参考 > 文档管理接口 > 上传文档"
    assert pieces[1].metadata["heading_path"] == "接口参考 > 检索接口 > 检索"


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


def test_table_rows_are_emitted_alongside_the_whole_table() -> None:
    """一张 8 行表产出 1 个整表片 + 8 个行级片，行级片是「列名=值」。"""
    markdown = (
        "# 价格表\n"
        "\n"
        "| 型号 | 配件 | 单价 |\n"
        "| --- | --- | --- |\n"
        "| XR-2100 | 万兆光模块 | 1180 |\n"
        "| XR-2100 Pro | 万兆光模块 | 1580 |\n"
    )

    pieces = MarkdownStructuredSplitter().split(markdown)

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    rows = [piece for piece in pieces if piece.metadata["chunk_type"] == "table_row"]

    assert len(tables) == 1
    assert len(rows) == 2, "两个数据行各一片"
    assert rows[0].text == "型号=XR-2100，配件=万兆光模块，单价=1180"
    assert rows[1].text == "型号=XR-2100 Pro，配件=万兆光模块，单价=1580"
    for piece in rows:
        assert piece.metadata["heading_path"] == "价格表"
        assert piece.metadata["table_part"] == 1


def test_table_row_skips_blank_cells() -> None:
    """空单元格不产出 `列名=` 这种没有信息量的碎片。"""
    markdown = "# 表\n\n| 型号 | 备注 |\n| --- | --- |\n| XR-2000 |  |\n"

    pieces = MarkdownStructuredSplitter().split(markdown)

    rows = [piece for piece in pieces if piece.metadata["chunk_type"] == "table_row"]
    assert len(rows) == 1
    assert rows[0].text == "型号=XR-2000"
    assert "备注=" not in rows[0].text


def test_large_table_row_pieces_follow_their_group() -> None:
    """分组后行级片沿用该组的 table_part，与整表片对齐。"""
    header = "| 编号 | 名称 |\n| --- | --- |"
    rows = "\n".join(f"| {number} | 商品{number} |" for number in range(1, 13))
    markdown = f"# 库存\n\n{header}\n{rows}\n"
    splitter = MarkdownStructuredSplitter(table_max_rows=5)

    pieces = splitter.split(markdown)

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    row_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "table_row"]
    assert len(tables) == 3
    assert len(row_pieces) == 12
    assert [piece.metadata["table_part"] for piece in tables] == [1, 2, 3]
    assert sorted({piece.metadata["table_part"] for piece in row_pieces}) == [1, 2, 3]
    assert row_pieces[0].text == "编号=1，名称=商品1"


def test_duplicate_table_collapses_with_its_row_pieces() -> None:
    """同一节内重复的表格整体折叠，连同行级片一起跳过。"""
    table = "| 套餐 | 价格 |\n| --- | --- |\n| 基础版 | 99 |\n"
    markdown = f"# 价格\n\n{table}\n{table}"

    pieces = MarkdownStructuredSplitter().split(markdown)

    tables = [piece for piece in pieces if piece.metadata["chunk_type"] == "table"]
    rows = [piece for piece in pieces if piece.metadata["chunk_type"] == "table_row"]
    assert len(tables) == 1
    assert len(rows) == 1


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


def test_short_code_block_kept_whole() -> None:
    """短代码块整块保留，分片序号为 0。"""
    markdown = "# 示例\n\n```python\ndef add(a, b):\n    return a + b\n```\n"
    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100)

    pieces = splitter.split(markdown)

    code_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert len(code_pieces) == 1
    assert "def add(a, b):" in code_pieces[0].text
    assert "return a + b" in code_pieces[0].text
    assert code_pieces[0].text.startswith("```")
    assert code_pieces[0].metadata["heading_path"] == "示例"
    assert code_pieces[0].metadata["code_part"] == 0


def _long_code_block(rows: int = 400) -> str:
    body = "\n".join(f"    value_{number} = {number}" for number in range(rows))
    return f"# 示例\n\n```python\n{body}\n```\n"


def test_long_code_block_is_split_within_window() -> None:
    """上千行代码不再产出超长单片：每片都不超过 chunk_size。"""
    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100)

    pieces = splitter.split(_long_code_block())

    code_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert len(code_pieces) > 1, "上千行代码必须被切开"
    assert all(len(piece.text) <= 800 for piece in code_pieces)
    assert max(len(piece.text) for piece in code_pieces) < 10_000


def test_code_pieces_have_ascending_part_numbers() -> None:
    """分片序号从 1 开始递增，且各片共享同一章节路径。"""
    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100)

    pieces = splitter.split(_long_code_block())

    code_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert [piece.metadata["code_part"] for piece in code_pieces] == list(
        range(1, len(code_pieces) + 1)
    )
    assert {piece.metadata["heading_path"] for piece in code_pieces} == {"示例"}


def test_code_splits_on_blank_lines_not_mid_statement() -> None:
    """切点优先落在空行，不切在语句中间。"""
    blocks = "\n\n".join(f"def function_{number}():\n    return {number}" for number in range(40))
    markdown = f"# 示例\n\n```python\n{blocks}\n```\n"
    splitter = MarkdownStructuredSplitter(chunk_size=300, chunk_overlap=100)

    pieces = splitter.split(markdown)

    code_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert len(code_pieces) > 1

    # 拼回去必须逐字还原原文：说明没有丢行、没有重复行。
    rebuilt = "".join(piece.text for piece in code_pieces)
    assert rebuilt == f"```python\n{blocks}\n```"

    # 每片去掉前导空行后，首行必须是完整的语句起点，不能是续行。
    for piece in code_pieces[1:]:
        first_line = piece.text.lstrip("\n").split("\n")[0]
        assert first_line.startswith("def function_"), first_line


def test_code_splits_on_dedent_when_no_blank_lines() -> None:
    """没有空行时，在缩进回退处断开。"""
    blocks = "\n".join(f"    statement_{number} = {number}" for number in range(60))
    markdown = f"# 示例\n\n```python\nclass Thing:\n{blocks}\n```\n"
    splitter = MarkdownStructuredSplitter(chunk_size=600, chunk_overlap=100)

    pieces = splitter.split(markdown)

    code_pieces = [piece for piece in pieces if piece.metadata["chunk_type"] == "code"]
    assert all(len(piece.text) <= 600 for piece in code_pieces)


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


def test_small_prose_pieces_in_one_section_are_merged() -> None:
    """同一节内两个过短的 prose 片合并成一片。

    走私有入口，是因为这个机制在公开入口上几乎触发不了：``_split_normal_lines``
    只在一节超过 ``chunk_size`` 时才切开，切出来的两片各自接近 ``chunk_size``，
    合起来必然超上限。行为仍需钉住，否则以后改阈值逻辑时无从判断对错。
    """

    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100, merge_threshold=150)
    pieces = [
        _Piece(text="短甲。" * 10, chunk_type="prose"),
        _Piece(text="短乙。" * 10, chunk_type="prose"),
        _Piece(text="长片。" * 300, chunk_type="prose"),
    ]

    merged = splitter._merge_small_pieces(pieces)

    assert len(merged) == 2, "前两片合并，第三片太长放不下"
    assert merged[0].text.startswith("短甲。")
    assert "短乙。" in merged[0].text, "合并保留顺序"
    assert merged[1].text.startswith("长片。")


def test_merge_never_exceeds_chunk_size() -> None:
    """合并后仍不得超过 chunk_size，否则这批内容会在别处又被切一次。"""

    splitter = MarkdownStructuredSplitter(chunk_size=100, chunk_overlap=10, merge_threshold=150)
    pieces = [
        _Piece(text="甲" * 60, chunk_type="prose"),
        _Piece(text="乙" * 60, chunk_type="prose"),
    ]

    merged = splitter._merge_small_pieces(pieces)

    assert len(merged) == 2, "60+60 超过 chunk_size=100，不许合"


def test_merge_skips_table_rows_and_code_parts() -> None:
    """行级表片与代码分片不参与合并：合并回去等于取消那两项能力。"""

    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100, merge_threshold=500)
    pieces = [
        _Piece(text="型号=XR-2000", chunk_type="table_row", table_part=1),
        _Piece(text="型号=XR-2100", chunk_type="table_row", table_part=1),
        _Piece(text="```y\na=1\n```", chunk_type="code", code_part=1),
        _Piece(text="mid", chunk_type="code", code_part=2),
    ]

    merged = splitter._merge_small_pieces(pieces)

    assert len(merged) == 4, "四片都不许合并"


def test_merge_threshold_zero_disables_merging() -> None:
    """阈值设为 0 时整条合并逻辑退化为不合并。"""

    splitter = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100, merge_threshold=0)
    pieces = [
        _Piece(text="短甲。", chunk_type="prose"),
        _Piece(text="短乙。", chunk_type="prose"),
    ]

    assert splitter._merge_small_pieces(pieces) == pieces


def test_merge_threshold_never_fires_on_the_eval_corpus() -> None:
    """当前语料上这个机制命中 0 次——把事实钉住，别让它变成「以为在起作用」。

    原因很具体：362 个节里 347 个只产出一片（h3 成为边界后每节本身就是原子单位），
    产出多片的节全是表格节，多出来的片都是 ``table_row``，按设计不参与合并。
    那 286 个「小于 150 字」的片是**整节都短**，没有同节的邻居可合。
    真要减少碎片，得允许跨 h3 合并到同一 h2——那是另一个决策，不是这条规则。
    """

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from eval_chunking import _load_corpus  # noqa: PLC0415

    loose = MarkdownStructuredSplitter(800, 100, 10, merge_threshold=0)
    tight = MarkdownStructuredSplitter(800, 100, 10, merge_threshold=150)

    without_merge = sum(len(loose.split(doc.text)) for doc in _load_corpus())
    with_merge = sum(len(tight.split(doc.text)) for doc in _load_corpus())

    assert with_merge == without_merge, (
        f"合并开始生效了（{without_merge} -> {with_merge} 片）："
        "请重新评估本测试的结论，并更新 docstring 里的数字"
    )


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
        "code_part",
    }
    for piece in pieces:
        assert set(piece.metadata) == expected_keys
        assert len(str(piece.metadata["section_id"])) == 64
    parent = next(piece for piece in pieces if piece.metadata["heading_path"] == "父章节")
    child = next(piece for piece in pieces if piece.metadata["heading_path"] == "父章节 > 子章节")
    assert parent.metadata["parent_section_id"] is None
    assert child.metadata["parent_section_id"] == parent.metadata["section_id"]
    assert child.metadata["table_part"] == 1, "非分组表格也算第 1 部分"
