"""Markdown 感知的结构化切块器。

与固定窗口（`WindowTextSplitter`）不同，这里先认标题层级、再在节内断句：
`#` / `##` / `###` 开启新节（一片一个子节，路径精确到子节），
`####` 及更深只加深 `heading_path`、不新起切片——「请求 / 响应 / 错误码」
这类附属小节因此不会被切得七零八落。

标题栈是整条链路里最容易写错的一处：flush 早一行，后面每个切片的
`heading_path` 都指向错误的章节，而且不会报任何错——只有检索结果会悄悄变差。
所以栈只在 h1/h2/h3 处 flush，且 flush 使用**遇到下一个 h1/h2/h3 之前的**栈内容。

`section_id` 刻意做成 `sha256(heading_path)` 的确定性哈希，而不是随机 ID：
重建索引后同一章节必须仍能对上父子关系，随机 ID 会让这条关系静默断裂。
"""

import hashlib
import re
from dataclasses import dataclass

from app.utils.provenance import page_from_heading
from app.utils.text_splitter import SplitPiece, TextSplitter
from app.utils.window_splitter import code_pieces as code_pieces_of
from app.utils.window_splitter import text_pieces as text_pieces_of

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s)")
_SEPARATOR_LINE = "---"


@dataclass(slots=True)
class _Block:
    """节内的一块内容：kind 是 normal / table / code，lines 是原始行。"""

    kind: str
    lines: list[str]
    # 代码块围栏上的语言标记（```````python`` → ``python``）；其它块为空串。
    language: str = ""


@dataclass(slots=True)
class _Piece:
    """一块内容切出来的一片文本及其分片序号。

    `table_part` 与 `code_part` 同构：``0`` 表示未分片，``1..n`` 表示第几段。
    两者刻意不复用同一个字段：同一节里表格与代码块可能同时存在，共用一个序号
    会让「按序拼回」失去边界（表格第 2 段与代码第 2 段无从区分）。
    """

    text: str
    chunk_type: str
    table_part: int = 0
    code_part: int = 0


class MarkdownStructuredSplitter(TextSplitter):
    """按 Markdown 标题结构切块，并产出可溯源的 metadata。

    标题栈驱动 provenance：h1/h2/h3 触发 section flush，h4+ 只更新栈。
    栈维护错一处不会抛异常，却会让其后每个切片的出处都错——改动这里必须用
    「标题边界」与「同输入 section_id 稳定」两类测试兜住。
    表格与代码块整块保留；超长节在节内二次切，重叠不跨标题边界。
    `section_id` 是 heading_path 的内容哈希，重建后父子关系仍能稳定对上。
    """

    # 切片边界到 h3。再深就不切了：h4 往往是「请求 / 响应 / 错误码」这类
    # 同一件事的附属小节，拆开会让每片失去上下文，只剩几个字段名。
    SECTION_LEVEL = 3

    def __init__(
        self,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
        table_max_rows: int = 10,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if table_max_rows < 1:
            raise ValueError("table_max_rows must be at least 1")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.table_max_rows = table_max_rows

    def split(self, text: str) -> list[SplitPiece]:
        """扫描全文，按 h1/h2/h3 边界产出一组带 provenance 的切片。"""
        if not text:
            return []
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")

        sections: list[tuple[str, list[_Block], int | None]] = []
        stack: list[tuple[int, str]] = []
        blocks: list[_Block] = []
        # 当前分节（h1/h2/h3）的路径，以及本节内出现过的深层标题路径（h4 及更深）。
        # 片覆盖的是整节，所以路径取「本节所有叶子标题」的公共前缀：
        #   - 单链 A > B > C  → 路径到 C（能到第 4 层）
        #   - 分叉 A > B1/B2  → 路径到 A（不能只写 B1，那样 B2 的内容被指错地方）
        section_path = ""
        deep_paths: list[str] = []
        # 物理页号：由「第 N 页」标题驱动，跨节一路带下去。它不能从 heading_path
        # 读回来，因为页标记与页内标题同级，在栈里会被后者顶掉。
        page: int | None = None

        def flush_section() -> None:
            nonlocal blocks, deep_paths
            sections.append(
                (_resolve_heading_path(section_path, deep_paths), blocks, page)
            )
            blocks = []
            deep_paths = []

        index = 0
        while index < len(lines):
            line = lines[index]
            heading = _HEADING_RE.match(line)
            if heading:
                level = len(heading.group(1))
                title = heading.group(2).strip()
                marker = page_from_heading(title)
                if marker is not None:
                    # 先 flush 上一节（它属于上一页），再切换页号——顺序反了会把
                    # 上一页的整节标到新页上。
                    flush_section()
                    page = marker
                    _push_heading(stack, level, title)
                    section_path = _join_stack(stack)
                    blocks.append(_Block(kind="normal", lines=[line]))
                    index += 1
                    continue
                if level <= self.SECTION_LEVEL:
                    flush_section()
                    _push_heading(stack, level, title)
                    section_path = _join_stack(stack)
                    blocks.append(_Block(kind="normal", lines=[line]))
                else:
                    _push_heading(stack, level, title)
                    deep_paths.append(_join_stack(stack))
                    _append_normal_line(blocks, line)
                index += 1
                continue
            if line.strip() == _SEPARATOR_LINE:
                index += 1
                continue
            if line.lstrip().startswith("```"):
                code_lines, index = _collect_code_block(lines, index)
                blocks.append(
                    _Block(
                        kind="code",
                        lines=code_lines,
                        language=_fence_language(code_lines[0]),
                    )
                )
                continue
            if "|" in line:
                table_lines, next_index = _collect_table_run(lines, index)
                if len(table_lines) >= 2:
                    blocks.append(_Block(kind="table", lines=table_lines))
                    index = next_index
                    continue
            _append_normal_line(blocks, line)
            index += 1

        flush_section()
        return self._assemble(sections)

    def _assemble(
        self, sections: list[tuple[str, list[_Block], int | None]]
    ) -> list[SplitPiece]:
        pieces: list[SplitPiece] = []
        for heading_path, blocks, page in sections:
            # 重复过滤只比较本节内紧邻的上一片；每节 flush 时重置基线。
            # 跨节重复的模板内容各有各的出处，不能因为文本相同就被静默删掉。
            last_text: str | None = None
            kept: list[_Piece] = []
            for piece in self._build_section_texts(blocks):
                if not piece.text.strip() or _is_heading_only(piece.text):
                    continue
                if piece.text == last_text:
                    continue
                kept.append(piece)
                last_text = piece.text
            for order_index, piece in enumerate(kept):
                pieces.append(
                    SplitPiece(
                        text=piece.text,
                        metadata=_metadata(
                            heading_path,
                            order_index,
                            piece.chunk_type,
                            piece.table_part,
                            piece.code_part,
                            page,
                        ),
                    )
                )
        return pieces

    def _build_section_texts(self, blocks: list[_Block]) -> list[_Piece]:
        produced: list[_Piece] = []
        # 同一节内出现过的表格文本，用于把重复表格整体折叠（连同行级片）。
        seen_tables: set[str] = set()
        for block in blocks:
            if block.kind == "code":
                # 未分片时序号为 0，分片后才从 1 开始编号——「0 表示整块」这条
                # 语义让消费方不用先数总数才知道该不该拼回。
                #
                # 剥掉首尾两根围栏行：每片应当是**纯代码**，否则 `from_language`
                # 的语法分隔符匹配不上行首（围栏行以 ``` 开头，会打断规则）。
                code_pieces = code_pieces_of(
                    _code_body(block.lines),
                    marker=block.language,
                    size=self.chunk_size,
                    overlap=self.chunk_overlap,
                )
                if len(code_pieces) == 1:
                    produced.append(_Piece(text=code_pieces[0], chunk_type="code"))
                else:
                    for code_part, text in enumerate(code_pieces, start=1):
                        produced.append(_Piece(text=text, chunk_type="code", code_part=code_part))
                continue
            if block.kind == "table":
                for table_part, group in enumerate(self._table_groups(block.lines), start=1):
                    table_text = "\n".join(group).strip("\n")
                    # 同一节内完全相同的表格整体跳过（连同它的行级片）。行级片插在
                    # 整表片之后，两片之间不再紧邻，_assemble 的相邻去重对表格失效，
                    # 所以去重必须按「表格组」在这里做，保持「同节重复表格折叠为一片」
                    # 的老行为。
                    if table_text in seen_tables:
                        continue
                    seen_tables.add(table_text)
                    produced.append(
                        _Piece(
                            text=table_text,
                            chunk_type="table",
                            table_part=table_part,
                        )
                    )
                    for row_text in _table_row_texts(group):
                        produced.append(
                            _Piece(
                                text=row_text,
                                chunk_type="table_row",
                                table_part=table_part,
                            )
                        )
                continue
            if block.kind == "normal":
                for text in text_pieces_of(
                    "\n".join(block.lines),
                    size=self.chunk_size,
                    overlap=self.chunk_overlap,
                ):
                    produced.append(_Piece(text=text, chunk_type=_classify_text(text)))
        return produced

    def _table_groups(self, rows: list[str]) -> list[list[str]]:
        """把表格切成若干组；每组顶部保留表头（header + 分隔行）。"""
        header_rows = 2 if len(rows) >= 2 and _is_table_separator(rows[1]) else 1
        header = rows[:header_rows]
        data_rows = rows[header_rows:]
        if not data_rows or len(data_rows) <= self.table_max_rows:
            return [rows]
        return [
            [*header, *data_rows[start : start + self.table_max_rows]]
            for start in range(0, len(data_rows), self.table_max_rows)
        ]


def _resolve_heading_path(section_path: str, deep_paths: list[str]) -> str:
    """把「分节路径 + 本节内的深层标题路径」合成该切片真正该标注的路径。

    规则：先剔除被更长路径覆盖的父级（``A > B`` 被 ``A > B > C`` 覆盖），剩下的
    就是叶子标题；再取它们的公共前缀。

    * 单链 ``A > B > C``：叶子只有 C，路径到 C——深层嵌套因此能真正体现在路径里；
    * 分叉 ``A > B1`` 与 ``A > B2``：公共前缀到 A——不能只写 B1，否则 B2 的内容
      会被标注到错误章节，而且不会报错，只有溯源结果会悄悄指错地方。
    """

    if not deep_paths:
        return section_path
    leaves = [
        path
        for path in deep_paths
        if not any(other != path and other.startswith(path + " > ") for other in deep_paths)
    ]
    if not leaves:
        leaves = deep_paths
    segments = [path.split(" > ") for path in leaves]
    prefix: list[str] = []
    for parts in zip(*segments):
        if len(set(parts)) != 1:
            break
        prefix.append(parts[0])
    joined = " > ".join(prefix)
    return joined or section_path


def _table_cells(line: str) -> list[str]:
    """把 `| a | b |` 拆成 `["a", "b"]`；首尾竖线不算空单元格。"""

    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _table_row_texts(group: list[str]) -> list[str]:
    """把一组表格的每个数据行拼成「列名=值，列名=值」。

    整表片回答「对比这几个型号」这类问题，行级片回答「XR-2100 的光模块多少钱」
    这类问题。后者若只给一行裸数据，向量还得靠表头才知道每列是什么；拼上列名
    之后，单元格自己就带语义，脱离表头也能读懂。

    表头与分隔行不是数据；空单元格（含行尾多出来的空段）不产出 `列名=`，
    否则会造出「配件=」这种没有信息量的噪声。
    """

    header_rows = 2 if len(group) >= 2 and _is_table_separator(group[1]) else 1
    headers = _table_cells(group[0])
    texts: list[str] = []
    for row in group[header_rows:]:
        cells = _table_cells(row)
        pairs: list[str] = []
        for position, value in enumerate(cells):
            if not value:
                continue
            name = headers[position] if position < len(headers) else ""
            pairs.append(f"{name}={value}" if name else value)
        if pairs:
            texts.append("，".join(pairs))
    return texts





def _push_heading(stack: list[tuple[int, str]], level: int, title: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))


def _join_stack(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _fence_language(open_fence: str) -> str:
    """从开围栏行取语言标记：```````python`` → ``python``；``````` `` → 空串。

    只取第一个词：```````python title="x"`` 这类写法里，语言总在最前面。
    """

    body = open_fence.lstrip()[3:].strip()
    return body.split()[0].lower() if body else ""


def _code_body(lines: list[str]) -> str:
    """剥掉首尾围栏，返回纯代码文本。

    保留围栏会让每片都以 ```` ``` ```` 开头，`from_language` 的语法分隔符按行首
    匹配，围栏行会把规则打断——于是长代码块可能一片都不切。
    """

    if not lines:
        return ""
    head = lines[1:] if lines[0].lstrip().startswith("```") else lines
    tail = head[:-1] if head and head[-1].lstrip().startswith("```") else head
    return "\n".join(tail)


def _collect_code_block(lines: list[str], start: int) -> tuple[list[str], int]:
    """从开围栏行收集到闭围栏行；未闭合时保留到文末。"""
    index = start + 1
    while index < len(lines):
        if lines[index].lstrip().startswith("```"):
            index += 1
            break
        index += 1
    return lines[start:index], index


def _collect_table_run(lines: list[str], start: int) -> tuple[list[str], int]:
    """收集连续含 `|` 的行；空行、标题、分隔线会截断表格。"""
    run: list[str] = []
    index = start
    while index < len(lines):
        candidate = lines[index]
        if not candidate.strip():
            break
        if _HEADING_RE.match(candidate) or candidate.strip() == _SEPARATOR_LINE:
            break
        if candidate.lstrip().startswith("```") or "|" not in candidate:
            break
        run.append(candidate)
        index += 1
    return run, index


def _is_table_separator(line: str) -> bool:
    """判断第二行是否为 `| --- | --- |` 这类分隔行。"""
    stripped = line.strip()
    return bool(stripped) and "-" in stripped and all(ch in "|-: \t" for ch in stripped)


def _append_normal_line(blocks: list[_Block], line: str) -> None:
    if blocks and blocks[-1].kind == "normal":
        blocks[-1].lines.append(line)
    else:
        blocks.append(_Block(kind="normal", lines=[line]))


def _is_heading_only(text: str) -> bool:
    lines = [line for line in text.split("\n") if line.strip()]
    return bool(lines) and all(_HEADING_RE.match(line) for line in lines)


def _classify_text(text: str) -> str:
    """按非空行多数派分类：列表行占比过半才算 list，其余算 prose。"""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return "prose"
    list_lines = sum(1 for line in lines if _LIST_LINE_RE.match(line))
    return "list" if list_lines > len(lines) / 2 else "prose"


def _section_segments(heading_path: str) -> list[str]:
    return heading_path.split(" > ") if heading_path else []


def _metadata(
    heading_path: str,
    order_index: int,
    chunk_type: str,
    table_part: int,
    code_part: int = 0,
    page: int | None = None,
) -> dict[str, object]:
    segments = _section_segments(heading_path)
    section_id = hashlib.sha256(heading_path.encode("utf-8")).hexdigest()
    parent_section_id = (
        hashlib.sha256(" > ".join(segments[:-1]).encode("utf-8")).hexdigest()
        if len(segments) >= 2
        else None
    )
    return {
        "heading_path": heading_path,
        "section_id": section_id,
        "parent_section_id": parent_section_id,
        "order_index": order_index,
        "chunk_type": chunk_type,
        "table_part": table_part,
        "code_part": code_part,
        "page": page,
    }
