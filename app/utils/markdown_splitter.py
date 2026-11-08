"""Markdown 感知的结构化切块器。

与 `CharacterTextSplitter` 的字符窗口不同，这里先认标题层级、再在节内断句：
`#` / `##` 开启新节，`###` 及更深只加深 `heading_path`、不新起切片，
这样一个小标题下的一句话不会变成孤零零的碎片。

标题栈是整条链路里最容易写错的一处：flush 早一行，后面每个切片的
`heading_path` 都指向错误的章节，而且不会报任何错——只有检索结果会悄悄变差。
所以栈只在 h1/h2 处 flush，且 flush 使用**遇到下一个 h1/h2 之前的**栈内容。

`section_id` 刻意做成 `sha256(heading_path)` 的确定性哈希，而不是随机 ID：
重建索引后同一章节必须仍能对上父子关系，随机 ID 会让这条关系静默断裂。
"""

import hashlib
import re
from dataclasses import dataclass

from app.utils.text_splitter import SplitPiece, TextSplitter

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s)")
_SEPARATOR_LINE = "---"


@dataclass(slots=True)
class _Block:
    """节内的一块内容：kind 是 normal / table / code，lines 是原始行。"""

    kind: str
    lines: list[str]


class MarkdownStructuredSplitter(TextSplitter):
    """按 Markdown 标题结构切块，并产出可溯源的 metadata。

    标题栈驱动 provenance：h1/h2 触发 section flush，h3+ 只更新栈。
    栈维护错一处不会抛异常，却会让其后每个切片的出处都错——改动这里必须用
    「标题边界」与「同输入 section_id 稳定」两类测试兜住。
    表格与代码块整块保留；超长节在节内二次切，重叠不跨标题边界。
    `section_id` 是 heading_path 的内容哈希，重建后父子关系仍能稳定对上。
    """

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
        """扫描全文，按 h1/h2 边界产出一组带 provenance 的切片。"""
        if not text:
            return []
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")

        sections: list[tuple[str, list[_Block]]] = []
        stack: list[tuple[int, str]] = []
        blocks: list[_Block] = []
        # 当前分节（h1/h2）的路径，以及本节内出现过的深层标题路径（h3 及更深）。
        # 片覆盖的是整节，所以路径取「本节所有叶子标题」的公共前缀：
        #   - 单链 A > B > C  → 路径到 C（能到第 4 层）
        #   - 分叉 A > B1/B2  → 路径到 A（不能只写 B1，那样 B2 的内容被指错地方）
        section_path = ""
        deep_paths: list[str] = []

        def flush_section() -> None:
            nonlocal blocks, deep_paths
            sections.append((_resolve_heading_path(section_path, deep_paths), blocks))
            blocks = []
            deep_paths = []

        index = 0
        while index < len(lines):
            line = lines[index]
            heading = _HEADING_RE.match(line)
            if heading:
                level = len(heading.group(1))
                title = heading.group(2).strip()
                if level <= 2:
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
                blocks.append(_Block(kind="code", lines=code_lines))
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

    def _assemble(self, sections: list[tuple[str, list[_Block]]]) -> list[SplitPiece]:
        pieces: list[SplitPiece] = []
        for heading_path, blocks in sections:
            # 重复过滤只比较本节内紧邻的上一片；每节 flush 时重置基线。
            # 跨节重复的模板内容各有各的出处，不能因为文本相同就被静默删掉。
            last_text: str | None = None
            kept: list[tuple[str, str, int]] = []
            for text, chunk_type, table_part in self._build_section_texts(blocks):
                if not text.strip() or _is_heading_only(text):
                    continue
                if text == last_text:
                    continue
                kept.append((text, chunk_type, table_part))
                last_text = text
            for order_index, (text, chunk_type, table_part) in enumerate(kept):
                pieces.append(
                    SplitPiece(
                        text=text,
                        metadata=_metadata(heading_path, order_index, chunk_type, table_part),
                    )
                )
        return pieces

    def _build_section_texts(self, blocks: list[_Block]) -> list[tuple[str, str, int]]:
        produced: list[tuple[str, str, int]] = []
        for block in blocks:
            if block.kind == "code":
                produced.append(("\n".join(block.lines).strip("\n"), "code", 0))
                continue
            if block.kind == "table":
                for table_part, group in enumerate(self._table_groups(block.lines), start=1):
                    produced.append(("\n".join(group).strip("\n"), "table", table_part))
                continue
            if block.kind == "normal":
                for text in self._split_normal_lines(block.lines):
                    produced.append((text, _classify_text(text), 0))
        return produced

    def _split_normal_lines(self, lines: list[str]) -> list[str]:
        """节内二次切：超长正文按 chunk_size 开窗，重叠只在节内保留。"""
        text = "\n".join(lines).strip("\n")
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        pieces: list[str] = []
        start = 0
        length = len(text)
        while start < length:
            end = min(start + self.chunk_size, length)
            if end < length:
                newline = text.rfind("\n", start + self.chunk_size // 4, end)
                if newline > start:
                    end = newline
            chunk = text[start:end].strip("\n")
            if chunk:
                pieces.append(chunk)
            if end >= length:
                break
            start = max(end - self.chunk_overlap, start + 1)
        return pieces

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


def _push_heading(stack: list[tuple[int, str]], level: int, title: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))


def _join_stack(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


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
    }
