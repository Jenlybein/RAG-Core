"""关系边抽取（纯函数层）：三种边、只认显式表达、有歧义就不建（ADR-0027 / 票 #152）。

这一层**不碰数据库、不发网络、不看问句**：输入是「这一份文档在某个版本里的节与正文」+
「该版本里已有的节目录」，输出是一组待写入的有向边。所有「哪条该建、哪条不该建」的判断
都在这里，于是它可以被穷举用例钉住，而接线层（:mod:`app.services.section_edge_service`）
只剩「读目录、删旧边、写新边」。

## 节点是「节」，不是切片

一条边的两端是一个**节**：``(租户, 库, 版本, 文档 id, 节 id)``。节 id 由
:func:`app.utils.section_identity.section_id_of` 算——本模块**不自己拼哈希**：两份算法
漂移的症状是「边指向一个存在但不对的节」，不报错、极难查。

## 三种边，各有各的识别规则

| 边型 | 规则 | 不建的情形 |
| --- | --- | --- |
| ``reference`` | ``《标题》第 N 节``、``本文件第 N 节``、Markdown 链接、同文档锚点 | 文档名对上两篇、节号对上两节、节号解析不出、目标节不在本版本、没写节号 |
| ``sibling`` | 同一父标题路径下**相邻**的两节（按节的文档顺序） | 父标题路径不同；不相邻（2.1 与 2.3 不直接连） |
| ``parent`` | 子节的标题路径去掉最后一段 = 父节的路径 | 没有上一级；父节只有标题（本版本里不产切片，那个 id 不存在） |

**为什么不按语义相似度补边**：那会在回答里长成假引用（可回溯性反而下降），而且「这两节
语义相近」没有稳定口径、无法验收。宁缺毋滥（ADR-0027 决策 3）。

**兄弟边为什么存两行**：兄弟是对称关系，但注入侧只按 ``source`` 查「我该拉谁进来」。
只存 左→右 的话，右邻居永远找不到左邻居——那半边边等于不存在。

## 路标切片（``reference_pointer``）

一句除了引用之外没有实质内容的话（「详见《促销规则》2.4 节。」），标成
:data:`CHUNK_TYPE_REFERENCE_POINTER`：**只用于建边，不参与召回**。它没有信息量，进候选池
只是占名额，还会把「命中一句『参见别处』」变成一次成功的召回。判定在
:func:`is_reference_pointer`：删掉全部显式引用表达之后剩下的字符少于
:data:`POINTER_RESIDUAL_CHARS` 个就算——**不猜语义**，只看「删完还剩什么」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Iterable, Mapping, Sequence

from app.models.section_edge import RELATION_PARENT, RELATION_REFERENCE, RELATION_SIBLING
from app.utils.section_identity import parent_heading_path, parent_section_id_of

#: 纯路标切片的类型名。召回侧按它过滤（向量 SQL 与关键词查询各一处），所以只此一份。
CHUNK_TYPE_REFERENCE_POINTER: Final[str] = "reference_pointer"

#: 删掉引用表达后剩余的可读字符少于此数 → 这一句只是路标。取值依据见
#: :func:`is_reference_pointer`（「详见」2 字 / 「参见」2 字 / 空串都算；「三个工作日内到账」
#: 8 字不算）。
POINTER_RESIDUAL_CHARS: Final[int] = 6

#: 章节号的两种写法：阿拉伯（``2`` / ``2.4``）与中文序数（``三`` / ``十二``）。
_NUMERAL = r"[0-9]+(?:\.[0-9]+)*|[一二三四五六七八九十百]+"

#: ``《标题》第 N 节``：跨文档引用的唯一识别形式（标题必须在书名号里）。
_TITLED_REFERENCE_RE = re.compile(
    rf"《(?P<title>[^》\n]{{1,80}})》\s*(?:第\s*)?(?P<number>{_NUMERAL})\s*节"
)

#: ``本文件第 N 节``：同文档引用。**「本文件」三个字是必须的**——光写「第 3 节」时
#: 无法判断指本文还是别处，那属于歧义（见 :func:`extract_reference_candidates`）。
_SAME_DOCUMENT_REFERENCE_RE = re.compile(
    rf"(?:本文件|本文|本篇)(?:的)?\s*第\s*(?P<number>{_NUMERAL})\s*节"
)

#: Markdown 链接 / 图片：``[文本](目标)``。目标可以是同文档锚点（``#2-4``）或另一个文件。
_MARKDOWN_LINK_RE = re.compile(r"!?\[(?P<text>[^\]\n]*)\]\((?P<target>[^)\s]+)\)")

#: 锚点与标题比较前的归一化：只留字母、数字与汉字，全部小写。
_ANCHOR_NOISE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


#: 章节号在标题里的位置：标题（或路径最后一段）以这个号开头，号后面不能再接数字或点。
def _number_prefix_re(number: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(number)}(?![0-9.])")


@dataclass(frozen=True)
class EdgeScope:
    """边的数据范围：一次构建产物就是这三列（ADR-0025 / ADR-0027 决策 1）。"""

    tenant_id: str
    kb_id: str
    index_version: str


@dataclass(frozen=True)
class SectionRecord:
    """一个节在某个版本里的身份行（目录的一行）。

    ``order`` 是**本文档内**的全局顺序（切片的 ``chunk_index``）：兄弟边与「第 N 节」都
    按它排。它不能省：``order_index`` 只在节内可比，跨节排不出文档顺序。
    """

    tenant_id: str
    kb_id: str
    index_version: str
    document_id: str
    document_title: str
    section_id: str
    heading_path: str
    order: int

    @property
    def scope(self) -> EdgeScope:
        return EdgeScope(
            tenant_id=self.tenant_id, kb_id=self.kb_id, index_version=self.index_version
        )

    @property
    def heading_title(self) -> str:
        """标题路径的最后一段（章节自己的标题）。"""

        return self.heading_path.split(" > ")[-1] if self.heading_path else ""


@dataclass(frozen=True)
class SectionText:
    """一片正文及其所属的节。引用是从**正文**里抽的，所以两者必须一起给。"""

    record: SectionRecord
    text: str


@dataclass(frozen=True)
class SectionCatalog:
    """某个版本里**已有的节**（解析目标用）。

    「已有的节」= 该版本里至少有一片正文的那些节。父节只有标题、正文全在子节里时它不在
    目录里（切块器不产那一片），于是指向它的边不会被建出来——**这不是缺点**：那个 id 在
    本版本里根本没有可检索的内容，建出来的是一条悬空边（ADR-0027「宁可少一条」）。
    """

    sections: tuple[SectionRecord, ...]

    @classmethod
    def of(
        cls, records: Iterable[SectionRecord], *, scope: EdgeScope | None = None
    ) -> "SectionCatalog":
        """建目录：按 ``(文档 id, 节 id)`` 去重，保留最先出现的 ``order``。

        给了 ``scope`` 就**只留该作用域里的节**——跨租户 / 跨库 / 跨版本的节连目录都进不来，
        于是「两端必须同作用域」这条规则有一半是在构造处成立的（另一半在
        :func:`extract_section_edges` 里逐条检查）。
        """

        kept: dict[tuple[str, str], SectionRecord] = {}
        for record in records:
            if scope is not None and record.scope != scope:
                continue
            key = (record.document_id, record.section_id)
            existing = kept.get(key)
            if existing is None or record.order < existing.order:
                kept[key] = record
        ordered = sorted(kept.values(), key=lambda r: (r.document_id, r.order, r.section_id))
        return cls(sections=tuple(ordered))

    def of_document(self, document_id: str) -> tuple[SectionRecord, ...]:
        """这份文档的节，按文档顺序。"""

        return tuple(
            sorted(
                (record for record in self.sections if record.document_id == document_id),
                key=lambda r: (r.order, r.section_id),
            )
        )

    def documents_titled(self, title: str) -> tuple[str, ...]:
        """标题**完全等于** ``title`` 的文档 id（去重、保序）。多于一个即歧义。"""

        wanted = title.strip()
        seen: list[str] = []
        for record in self.sections:
            if record.document_title.strip() == wanted and record.document_id not in seen:
                seen.append(record.document_id)
        return tuple(seen)

    def section_by_id(self, *, document_id: str, section_id: str) -> SectionRecord | None:
        for record in self.of_document(document_id):
            if record.section_id == section_id:
                return record
        return None

    def section_by_number(self, *, document_id: str, number: str) -> SectionRecord | None:
        """章节号 → 节。**对上零个或两个以上都回 ``None``**（解析不出 / 有歧义）。

        阿拉伯号（``2.4``）：标题以这个号开头即命中，要求恰好一个。
        中文序数（``三``）：先看有没有标题自带编号 ``3``；没有则按文档顺序取第 3 节。
        两条规则打架时以**作者写下的编号**为准——「第 3 节」在带编号的文档里指的就是
        「3 那节」，按位置数反而容易错开一行。
        """

        sections = self.of_document(document_id)
        number = number.strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", number):
            return _unique_match(sections, number)
        ordinal = chinese_number_to_int(number)
        if ordinal is None:
            return None
        numbered = _unique_match(sections, str(ordinal))
        if numbered is not None:
            return numbered
        if _has_numbered_heading(sections, str(ordinal)):
            # 有编号但那条编号重复（_unique_match 回 None）：歧义，不猜。
            return None
        if 1 <= ordinal <= len(sections):
            return sections[ordinal - 1]
        return None

    def section_by_anchor(self, *, document_id: str, anchor: str) -> SectionRecord | None:
        """Markdown 锚点 → 节。按标题文本归一化后比较，命中多个即歧义。"""

        wanted = _normalize_anchor(anchor)
        if not wanted:
            return None
        sections = self.of_document(document_id)
        matches = [
            record for record in sections if _normalize_anchor(record.heading_title) == wanted
        ]
        if not matches:
            matches = [
                record for record in sections if _normalize_anchor(record.heading_path) == wanted
            ]
        if len(matches) != 1:
            return None
        return matches[0]


def _unique_match(sections: Sequence[SectionRecord], number: str) -> SectionRecord | None:
    """标题以该章节号开头的节；恰好一个才算命中。"""

    pattern = _number_prefix_re(number)
    matches = [record for record in sections if pattern.match(record.heading_title.strip())]
    if len(matches) != 1:
        return None
    return matches[0]


def _has_numbered_heading(sections: Sequence[SectionRecord], number: str) -> bool:
    pattern = _number_prefix_re(number)
    return any(pattern.match(record.heading_title.strip()) for record in sections)


def _normalize_anchor(text: str) -> str:
    return _ANCHOR_NOISE_RE.sub("", text.lower())


def chinese_number_to_int(text: str) -> int | None:
    """``三`` → 3、``十二`` → 12、``二十`` → 20；看不懂就 ``None``（不猜序号）。

    只覆盖「百以内」——章节号现实里就是这个量级，写一个完整的汉语数字解析器不如把
    看不懂的写法明确判成「解析不出」，那正是本票要的边界（歧义不建边）。
    """

    digits = {digit: value for value, digit in enumerate("零一二三四五六七八九")}
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "百" in text:
        return None
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = 1 if head == "" else digits.get(head)
        ones = 0 if tail == "" else digits.get(tail)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(text) == 1:
        return digits.get(text)
    return None


@dataclass(frozen=True)
class ReferenceCandidate:
    """从正文里认出来的一条显式引用表达（还没解析成节）。

    ``expression`` 是原文片段、``sentence`` 是它所在的那一句——两者都进边的 ``metadata``，
    排障时先看这两项（「这条边凭什么建出来的」）。
    """

    kind: str
    expression: str
    sentence: str
    start: int
    end: int
    document_title: str | None = None
    section_number: str | None = None
    anchor: str | None = None


@dataclass(frozen=True)
class EdgeDraft:
    """待写入的一条有向边（作用域 + 两端 + 边型 + 证据）。"""

    tenant_id: str
    kb_id: str
    index_version: str
    source_document_id: str
    source_section_id: str
    target_document_id: str
    target_section_id: str
    relation_type: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def unique_key(self) -> tuple[str, ...]:
        """与库里那条八列唯一约束同形状：去重靠它，写入侧也靠它。"""

        return (
            self.tenant_id,
            self.kb_id,
            self.index_version,
            self.source_document_id,
            self.source_section_id,
            self.target_document_id,
            self.target_section_id,
            self.relation_type,
        )

    @property
    def is_self_loop(self) -> bool:
        return (
            self.source_document_id == self.target_document_id
            and self.source_section_id == self.target_section_id
        )


# ───────────────────────────────────────────────────── 显式表达 → 候选


def extract_reference_candidates(text: str) -> tuple[ReferenceCandidate, ...]:
    """从一段正文里认出所有**显式引用表达**（按出现顺序）。

    四类，都是「文档作者亲自指的线」：

    * ``《标题》第 N 节`` / ``《标题》2.4 节`` —— 跨文档；
    * ``本文件第 N 节`` / ``本文第三节`` —— 同文档（必须是「本文件」这类限定语）；
    * ``[文本](#锚点)`` —— 同文档锚点；
    * ``[文本](文件名.md)`` / ``[文本](文件名.md#锚点)`` —— 跨文档链接（按文件名匹配标题）；
    * ``[文本](#锚点)`` 之外的同文档纯锚点 ``[文本](#标题)`` 同上。

    裸的「第 3 节」**不在这里**：它既可能指本文也可能指别处，属于歧义。
    """

    if not text:
        return ()
    candidates: list[ReferenceCandidate] = []
    for match in _TITLED_REFERENCE_RE.finditer(text):
        candidates.append(
            ReferenceCandidate(
                kind="title",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                document_title=match.group("title").strip(),
                section_number=match.group("number"),
            )
        )
    for match in _SAME_DOCUMENT_REFERENCE_RE.finditer(text):
        candidates.append(
            ReferenceCandidate(
                kind="same_document",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                section_number=match.group("number"),
            )
        )
    for match in _MARKDOWN_LINK_RE.finditer(text):
        target = match.group("target").strip()
        expression = match.group(0)
        sentence = _sentence_around(text, match.start(), match.end())
        path, _, anchor = target.partition("#")
        if not path:
            if not anchor:
                continue
            candidates.append(
                ReferenceCandidate(
                    kind="same_document",
                    expression=expression,
                    sentence=sentence,
                    start=match.start(),
                    end=match.end(),
                    anchor=anchor,
                )
            )
            continue
        title = _document_title_from_link(path)
        if not title:
            continue
        candidates.append(
            ReferenceCandidate(
                kind="title",
                expression=expression,
                sentence=sentence,
                start=match.start(),
                end=match.end(),
                document_title=title,
                anchor=anchor or None,
            )
        )
    candidates.sort(key=lambda candidate: candidate.start)
    return tuple(candidates)


def _document_title_from_link(path: str) -> str:
    """链接目标 → 文档标题：取最后一段，去掉扩展名。

    没有「URL → 文档」的映射（文档是上传进来的文件，不是一张站点地图），所以只能按
    文件名与标题做**完全匹配**；对不上就是不建边，而不是猜最像的那篇。
    """

    name = path.rstrip("/").split("/")[-1]
    stem, dot, extension = name.rpartition(".")
    if dot and extension and len(extension) <= 5:
        return stem.strip()
    return name.strip()


def _sentence_around(text: str, start: int, end: int) -> str:
    """取命中所在的句子（按中文句读 / 换行切），供 metadata 留证。

    「所在的句子」= 命中之前最近的一个句读之后，到命中之后最近的一个句读之前。别把整段
    正文塞进 metadata：证据要短到人人能一眼核对。
    """

    boundaries = "。！？；!?;\n"
    left = max((text.rfind(mark, 0, start) for mark in boundaries), default=-1) + 1
    right_starts = [text.find(mark, end) for mark in boundaries]
    ends = [position + 1 for position in right_starts if position != -1]
    right = min(ends) if ends else len(text)
    return text[left:right].strip()


def reference_spans(text: str) -> tuple[tuple[int, int], ...]:
    """正文里全部引用表达覆盖的区间（合并重叠），供路标判定删用。"""

    spans = [(candidate.start, candidate.end) for candidate in extract_reference_candidates(text)]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    return tuple(merged)


def is_reference_pointer(text: str) -> bool:
    """这一片是不是**纯路标**（除了引用之外没有实质内容）。

    判据是「删掉全部显式引用表达之后还剩多少可读字符」：少于
    :data:`POINTER_RESIDUAL_CHARS` 个就算。它不猜语义、不看长度、不看是否列表——
    「详见《X》2.4 节。」剩 2 个字（详见）→ 路标；「退款超期需人工审核，详见《X》2.4 节。」
    剩 9 个字 → 不是路标（它自己就回答得了问题）。没有任何引用表达时一律不是路标。
    """

    if not text or not text.strip():
        return False
    spans = reference_spans(text)
    if not spans:
        return False
    residual: list[str] = []
    cursor = 0
    for start, end in spans:
        residual.append(text[cursor:start])
        cursor = end
    residual.append(text[cursor:])
    readable = _ANCHOR_NOISE_RE.sub("", "".join(residual))
    return len(readable) < POINTER_RESIDUAL_CHARS


# ───────────────────────────────────────────────────── 三种边的抽取


def extract_section_edges(
    *,
    scope: EdgeScope,
    document_id: str,
    document_title: str,
    sections: Sequence[SectionText],
    catalog: SectionCatalog,
) -> tuple[EdgeDraft, ...]:
    """这份文档在本版本里的全部出边（按 :data:`EdgeDraft.unique_key` 去重）。

    ``sections`` 是**这一份文档**在这个版本里的切片（每片带所属的节）。引用从正文里抽，
    兄弟 / 父节从节的标题路径里抽——两者都要求这一节真的在本版本里存在（它就是刚写进去
    的那些切片），于是不会出现「源节不存在」的边。

    ``catalog`` 是**解析目标**用的节目录：目标节不在里面（歧义 / 不在本版本 / 别的租户）
    就不建那条边。悬空边在这里被挡住，而不是等到检索时才发现。
    """

    drafts: dict[tuple[str, ...], EdgeDraft] = {}
    own_sections = _unique_sections(sections, scope=scope, document_id=document_id)
    if not own_sections:
        return ()

    def add(draft: EdgeDraft) -> None:
        if draft.is_self_loop:
            return
        drafts.setdefault(draft.unique_key, draft)

    # ① 引用：只从正文里认，目标必须在目录里解析成**唯一**一节。
    for entry in sections:
        record = entry.record
        if record.document_id != document_id or record.scope != scope:
            continue
        for candidate in extract_reference_candidates(entry.text):
            target = _resolve_candidate(
                candidate, scope=scope, document_id=document_id, catalog=catalog
            )
            if target is None:
                continue
            add(
                _draft(
                    scope=scope,
                    relation_type=RELATION_REFERENCE,
                    source=record,
                    target=target,
                    metadata={
                        "expression": candidate.expression,
                        "sentence": candidate.sentence,
                        "document_title": candidate.document_title,
                        "section_number": candidate.section_number,
                        "anchor": candidate.anchor,
                        "source_heading_path": record.heading_path,
                        "target_heading_path": target.heading_path,
                    },
                )
            )

    # ② 兄弟：同一父标题路径下**相邻**的两节，两头各一条（对称关系要有向存两行）。
    for group in _sibling_groups(own_sections):
        for left, right in zip(group, group[1:]):
            for source, target in ((left, right), (right, left)):
                add(
                    _draft(
                        scope=scope,
                        relation_type=RELATION_SIBLING,
                        source=source,
                        target=target,
                        metadata={
                            "parent_heading_path": parent_heading_path(source.heading_path),
                            "source_heading_path": source.heading_path,
                            "target_heading_path": target.heading_path,
                        },
                    )
                )

    # ③ 父节：标题层级。父节必须**本版本里真有这一节**（它自己有正文），否则那条边是悬空的。
    by_id = {record.section_id: record for record in own_sections}
    for record in own_sections:
        parent_id = parent_section_id_of(document_id=document_id, heading_path=record.heading_path)
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            continue
        add(
            _draft(
                scope=scope,
                relation_type=RELATION_PARENT,
                source=record,
                target=parent,
                metadata={
                    "source_heading_path": record.heading_path,
                    "target_heading_path": parent.heading_path,
                },
            )
        )

    return tuple(drafts.values())


def _draft(
    *,
    scope: EdgeScope,
    relation_type: str,
    source: SectionRecord,
    target: SectionRecord,
    metadata: Mapping[str, object],
) -> EdgeDraft:
    return EdgeDraft(
        tenant_id=scope.tenant_id,
        kb_id=scope.kb_id,
        index_version=scope.index_version,
        source_document_id=source.document_id,
        source_section_id=source.section_id,
        target_document_id=target.document_id,
        target_section_id=target.section_id,
        relation_type=relation_type,
        metadata=dict(metadata),
    )


def _unique_sections(
    sections: Sequence[SectionText], *, scope: EdgeScope, document_id: str
) -> tuple[SectionRecord, ...]:
    """本文档的节（按文档顺序、去重、只留本作用域）。"""

    kept: dict[str, SectionRecord] = {}
    for entry in sections:
        record = entry.record
        if record.document_id != document_id or record.scope != scope:
            continue
        existing = kept.get(record.section_id)
        if existing is None or record.order < existing.order:
            kept[record.section_id] = record
    return tuple(sorted(kept.values(), key=lambda record: (record.order, record.section_id)))


def _sibling_groups(
    sections: Sequence[SectionRecord],
) -> list[list[SectionRecord]]:
    """按父标题路径分组；组内按文档顺序。

    父标题路径为 ``None``（顶层节）的那些**同属一组**：它们的上一级是文档本身，
    彼此仍是兄弟。用「路径前缀」判断会把「2 退款 > 2.1」和「2 退款说明 > 2.1」错认成
    兄弟，所以分组键取 ``parent_heading_path`` 的规整值，不做字符串前缀匹配。
    """

    groups: dict[str, list[SectionRecord]] = {}
    for record in sections:
        key = parent_heading_path(record.heading_path) or ""
        groups.setdefault(key, []).append(record)
    return [
        sorted(group, key=lambda record: (record.order, record.section_id))
        for group in groups.values()
    ]


def _resolve_candidate(
    candidate: ReferenceCandidate,
    *,
    scope: EdgeScope,
    document_id: str,
    catalog: SectionCatalog,
) -> SectionRecord | None:
    """把一条候选解析成目标节；**任何一步不唯一就回 ``None``**（歧义不建边）。"""

    if candidate.kind == "same_document":
        target_document = document_id
    else:
        titled = catalog.documents_titled(candidate.document_title or "")
        if len(titled) != 1:
            return None
        target_document = titled[0]

    target: SectionRecord | None = None
    if candidate.anchor is not None:
        target = catalog.section_by_anchor(document_id=target_document, anchor=candidate.anchor)
    if candidate.section_number is not None:
        by_number = catalog.section_by_number(
            document_id=target_document, number=candidate.section_number
        )
        if (
            target is not None
            and by_number is not None
            and target.section_id != by_number.section_id
        ):
            # 锚点与章节号指向两节：这份表达自相矛盾，不猜。
            return None
        target = target or by_number
    if target is None and candidate.kind == "title" and candidate.section_number is None:
        # 链接只指到「哪一篇」、没指到「哪一节」：节点是节，所以取该篇**唯一的顶层节**。
        # 有多个顶层节时「整篇」对应哪一节无从判断——那是歧义，不建（ADR-0027 决策 1/3）。
        top_level = [
            record
            for record in catalog.of_document(target_document)
            if parent_heading_path(record.heading_path) is None
        ]
        if len(top_level) == 1:
            target = top_level[0]
    if target is None:
        return None
    if target.scope != scope:
        # 目录没按作用域构造时（调用方传了全量目录），这里再拦一次：跨租户 / 跨库 / 跨版本
        # 的边一条都不许建。关系本身不能成为越权通道。
        return None
    if target.document_id == document_id and target.section_id == "":
        return None
    return target


__all__ = [
    "CHUNK_TYPE_REFERENCE_POINTER",
    "POINTER_RESIDUAL_CHARS",
    "EdgeDraft",
    "EdgeScope",
    "ReferenceCandidate",
    "SectionCatalog",
    "SectionRecord",
    "SectionText",
    "chinese_number_to_int",
    "extract_reference_candidates",
    "extract_section_edges",
    "is_reference_pointer",
    "reference_spans",
]
