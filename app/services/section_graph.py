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

**Markdown 链接按文件名对目标，标题只是兜底**：链接写的是 ``./points_policy.md``，而标题是
《积分规则》——两者常常不同（``eval/corpus-refs`` 里三条链接全是这样）。所以先按
``documents.source_filename`` 的裸文件名（去扩展名、大小写不敏感）精确匹配，匹配不到才回落
「文件名主干 == 标题」。链接后面紧跟的「第 N 节」属于这条链接；两条都不唯一或都不命中时
不建边（歧义口径与其它表达一致）。

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
from typing import Callable, Final, Iterable, Mapping, Sequence

from app.models.chunk import CHUNK_TYPE_REFERENCE_POINTER
from app.models.section_edge import RELATION_PARENT, RELATION_REFERENCE, RELATION_SIBLING
from app.utils.section_identity import parent_heading_path, parent_section_id_of

#: 纯路标切片的类型名在模型层（那是**落库的值**，不是抽取规则的一部分）：
#: ``app.models.chunk.CHUNK_TYPE_REFERENCE_POINTER``。这里 re-export，抽取层与召回侧
#: 于是引用同一个名字，不存在「一侧叫 reference_pointer、另一侧叫 pointer」的漂移。

#: 删掉引用表达后剩余的可读字符少于此数 → 这一句只是路标。取值依据见
#: :func:`is_reference_pointer`（「详见」2 字 / 「参见」2 字 / 空串都算；「三个工作日内到账」
#: 8 字不算）。
POINTER_RESIDUAL_CHARS: Final[int] = 6

#: 章节号的两种写法：阿拉伯（``2`` / ``2.4`` / 全角``２``）与中文序数（``三`` / ``十二``）。
_NUMERAL = r"[0-9０-９]+(?:\.[0-9０-９]+)*|[一二三四五六七八九十百]+"

#: 同文档限定语：`本文第 2 条` / `本政策第 4 条` / `本纪要第 1 条`。**没有限定语的裸条号不认**
#: （`分摊口径见第 3 条` 既可能指本文也可能指别处，属于歧义）。
_DOCUMENT_QUALIFIERS = "本文件|本文|本篇|本政策|本纪要|本附件|本细则|本规则|本协议"

#: 编号定位符的两种形状：``第 M 章第 N 节``（章只是路径层）与单个 ``第 N 节/条/款/项``。
_CHAPTER_SECTION = rf"第\s*(?P<chapter>{_NUMERAL})\s*章\s*第\s*(?P<nested>{_NUMERAL})\s*[条节款项]"
_SINGLE_CLAUSE = rf"(?:第\s*)?(?P<number>{_NUMERAL})\s*[条节款项]"

#: ``《标题》第 N 节`` / ``《标题》第 N 条``：跨文档引用的唯一识别形式（标题必须在书名号里）。
_TITLED_REFERENCE_RE = re.compile(rf"《(?P<title>[^》\n]{{1,80}})》\s*(?:{_CHAPTER_SECTION}|{_SINGLE_CLAUSE})")

#: ``本文件第 N 节`` / ``本文第 N 条``：同文档引用。**限定语是必须的**——光写「第 3 节」时
#: 无法判断指本文还是别处，那属于歧义（见 :func:`extract_reference_candidates`）。
_SAME_DOCUMENT_REFERENCE_RE = re.compile(
    rf"(?:{_DOCUMENT_QUALIFIERS})(?:的)?\s*(?:{_CHAPTER_SECTION}|{_SINGLE_CLAUSE})"
)


#: Markdown 链接 / 图片：``[文本](目标)``。目标可以是同文档锚点（``#2-4``）或另一个文件。
_MARKDOWN_LINK_RE = re.compile(r"!?\[(?P<text>[^\]\n]*)\]\((?P<target>[^)\s]+)\)")

#: 链接后面紧跟的定位符（``](x.md)第 3 节`` / ``](x.md) 决策五`` / ``](x.md)「第三次修订」``）。
#: 链接本身只指到文件，节写在链接外面——不把它吃进来，语料里那三条链接就一条边都建不出来。
_LINK_LOCATOR_RE = re.compile(
    r"^\s*(?:"
    rf"{_CHAPTER_SECTION}"
    rf"|{_SINGLE_CLAUSE}"
    rf"|决策\s*(?P<decision>{_NUMERAL})"
    r"|[「『](?P<quoted>[^」』\n]{2,60})[」』]"
    r")"
)

#: 文档编号引用（``ADR-0013`` / ``ADR 0013``）：技术文档里指另一篇文档的常见写法。
_ADR_CODE_RE = re.compile(r"ADR[-\s]?([0-9]{1,4})", re.IGNORECASE)

#: 「决策 N」：ADR 里指决策条目的写法。**范围引用不算**（``决策 1–6`` 落不到唯一一节）。
_DECISION_REFERENCE_RE = re.compile(
    rf"决策\s*(?P<number>{_NUMERAL})(?!\s*[-‐–—~～至到]\s*[0-9０-９一二三四五六七八九十百])"
)

#: 英文附件的编号定位符：``Section 2``（大小写不敏感）。
_ENGLISH_SECTION_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9])Section\s+(?P<number>[0-9０-９]+)(?![0-9])(?!\.\d)", re.IGNORECASE
)

#: 裸 ``第 M 章第 N 节``（``详见第三章第二节``）：章 + 节两层编号本身就够具体，
#: 不要求「本文 / 本文件」限定语（ADR-0027 决策 8 把这一形态列进 R2）。
_BARE_CHAPTER_SECTION_RE = re.compile(_CHAPTER_SECTION)

#: 引号短语后面跟的「节 / 条」后缀也算同文档上下文（``「后果」一节``）。
_QUOTED_SUFFIX_MARKER_RE = re.compile(r"^\s*(?:这|最后)?[条节]|^\s*[里中]")

#: 锚点与标题比较前的归一化：只留字母、数字与汉字，全部小写。
_ANCHOR_NOISE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

# ───────────────────────────────────── 定位符（R1 / R2，ADR-0027 决策 8）

#: 全角数字 → 半角（真实政策文本里「第４节」很常见）。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

#: 标题名归一化：去标点、全角→半角、小写（比较两边都走它）。
_HEADING_NAME_NOISE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

#: 归一化里的一步：中文数字 → 阿拉伯（`「后续修订（第 7 步）」` 与标题里的 `第 7 步` 对齐）。
_CN_NUMERAL_RUN_RE = re.compile(r"[零一二三四五六七八九十百]+")

#: 引用动词：裸标题名只有紧跟它时才试（`见翻案条件`）。`与决策二` 这种陈述句不行。
_CITATION_VERBS = ("详见", "参见", "参照", "另见", "依据", "根据", "见", "按", "据", "依")

#: 引号括住的短语：这是「作者明确指了一个名字」的最强信号（`「后果」`）。
_QUOTED_HEADING_RE = re.compile(r"[「『]([^」』\n]{2,60})[」』]")

#: 同文档限定语里「明说这是本文」的那几个（引号标题名的同文档上下文也用它们）。
_SAME_DOCUMENT_MARKERS = re.compile(
    r"文末|本文|本文件|本篇|本政策|本纪要|本附件|本细则|本规则|本协议"
)


#: 章节号在标题里的位置：标题（或路径最后一段）以这个号开头，号后面不能再接数字或点。
def _number_prefix_re(number: str) -> re.Pattern[str]:
    """标题以这个章节号开头的判据：号后面不能再接数字，也不能接**后面还跟着数字的点**。

    「2」不该匹配「2.4 抵扣上限」（那是另一节），但**要**匹配「2. 积分抵扣与使用上限」
    ——「号 + 点 + 空格」是中文文档最常见的编号写法，把点也一起禁掉的话，语料里
    ``## 3. 退款到账与凭证`` 这种标题一节都解析不出来（#152 复核时踩到）。
    """

    return re.compile(rf"^{re.escape(number)}(?!\d)(?!\.\d)")


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

    ``source_filename`` 是上传时的原始文件名（``documents.source_filename`` 的快照）：
    Markdown 链接写的是**文件名**（``./points_policy.md``），而文档标题是给人看的
    （《积分规则》）——两者常常不同，靠标题去对文件名会一条边都建不出来。
    """

    tenant_id: str
    kb_id: str
    index_version: str
    document_id: str
    document_title: str
    section_id: str
    heading_path: str
    order: int
    source_filename: str = ""

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
        return self._documents_where(
            lambda record: record.document_title.strip() == wanted
        )

    def documents_with_code(self, code: str) -> tuple[str, ...]:
        """文档编号（``ADR-0025`` → ``0025``）→ 文档 id。多于一个或零个都由调用方弃权。

        ADR / RFC 一类的技术文档里，引用写的是**文档编号**（``（ADR-0025 决策五）``），
        而文档标题是 ``0025. 索引即构建产物…``——两者只共享开头那个编号。不按编号对，
        这类引用一条边都建不出来（ADR-0027 决策 8）。
        """

        wanted = to_arabic_number(code)
        if wanted is None:
            return ()
        return self._documents_where(lambda record: _document_code_of(record) == wanted)

    def documents_for_link_target(self, target: str) -> tuple[str, ...]:
        """Markdown 链接目标 → 文档 id：**先比文件名，再回落标题**。

        链接写的是文件名（``./points_policy.md``），标题是给人看的（《积分规则》）——
        两者常常不同。``documents.source_filename`` 是上传时的原始文件名，它才是链接
        天然对应的东西；标题只在没有文件名时兜底（老数据 / 纯文本上传的文档）。
        两次匹配都不唯一（或都不命中）时返回空 / 多个，由调用方按歧义处理。
        """

        stem = _link_stem(target)
        if not stem:
            return ()
        by_filename = self._documents_where(
            lambda record: _link_stem(record.source_filename) == stem
        )
        if by_filename:
            # 文件名命中就定下来：它比标题更接近作者写链接时的所指，不再用标题去猜第二遍。
            return by_filename
        return self.documents_titled(stem)

    def _documents_where(self, predicate: Callable[[SectionRecord], bool]) -> tuple[str, ...]:
        seen: list[str] = []
        for record in self.sections:
            if predicate(record) and record.document_id not in seen:
                seen.append(record.document_id)
        return tuple(seen)

    def section_by_id(self, *, document_id: str, section_id: str) -> SectionRecord | None:
        for record in self.of_document(document_id):
            if record.section_id == section_id:
                return record
        return None

    def section_by_number(self, *, document_id: str, number: str) -> SectionRecord | None:
        """章节号 → 节。**对上零个或两个以上都回 ``None``**（解析不出 / 有歧义）。

        阿拉伯号（``2`` / ``2.4``）：标题以这个号开头即命中，要求恰好一个；标题写成
        「第 2 条 X」「Section 2 X」的惯例也算——**``2`` 不匹配 ``2.1``**（那是另一节）。
        中文序数（``三``）：先看有没有标题自带编号 ``3``；没有则按文档顺序取第 3 节。
        两条规则打架时以**作者写下的编号**为准——「第 3 节」在带编号的文档里指的就是
        「3 那节」，按位置数反而容易错开一行。
        """

        sections = self.of_document(document_id)
        number = number.strip()
        if re.fullmatch(r"[0-9０-９]+(?:\.[0-9０-９]+)*", number.translate(_FULLWIDTH_DIGITS)):
            return _unique_by_number(sections, to_arabic_number(number) or number)
        ordinal = chinese_number_to_int(number)
        if ordinal is None:
            return None
        numbered = _unique_by_number(sections, str(ordinal))
        if numbered is not None:
            return numbered
        if _has_numbered_heading(sections, str(ordinal)):
            # 有编号但那条编号重复（_unique_by_number 回 None）：歧义，不猜。
            return None
        if _has_clause_heading(sections, str(ordinal)):
            # 有「第 N 条 / 第 N 节」标题但重复：歧义，不猜。
            return None
        if 1 <= ordinal <= len(sections):
            return sections[ordinal - 1]
        return None

    def section_by_heading_number(self, *, document_id: str, number: str) -> SectionRecord | None:
        """编号定位符（``决策 5`` / ``第 4 条`` / ``Section 2``）→ 标题编号恰为 N 的那一节。

        与 :meth:`section_by_number` 的区别：这里**只认标题自己写的编号**，不按位置数。
        「决策 N」在 ADR-0025 里对应「决策五：…」、在 ADR-0027 里对应「3. …」——两种惯例
        都靠 :func:`_heading_number_of` 归一。**唯一一条**才算命中（ADR-0027 决策 8）。
        """

        wanted = to_arabic_number(number)
        if wanted is None:
            return None
        matches = [
            record
            for record in self.of_document(document_id)
            if _heading_number_of(record.heading_title) == wanted
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def section_by_chapter_and_section(
        self, *, document_id: str, chapter: str, section: str
    ) -> SectionRecord | None:
        """``第 M 章第 N 节`` → 「章」在标题路径里、自身标题编号恰为 N 的那一节。

        章只是**路径上的一层**（章标题自己常常没有正文、不在目录里），所以章编号只能在
        ``heading_path`` 里找；节编号在自身标题里找。两个编号都对上且唯一才算命中。
        """

        chapter_number = to_arabic_number(chapter)
        section_number = to_arabic_number(section)
        if chapter_number is None or section_number is None:
            return None
        matches: list[SectionRecord] = []
        for record in self.of_document(document_id):
            segments = record.heading_path.split(" > ") if record.heading_path else []
            if not any(_chapter_number_of(segment) == chapter_number for segment in segments):
                continue
            if _heading_number_of(record.heading_title) == section_number:
                matches.append(record)
        if len(matches) != 1:
            return None
        return matches[0]

    def section_by_heading_name(self, *, document_id: str, name: str) -> SectionRecord | None:
        """标题名引用（``「后果」`` / ``见翻案条件``）→ 唯一一条**等于或前缀于**它的节标题。

        归一（去标点 / 全角→半角 / 中文数字→阿拉伯）后比较；命中 0 条或 >1 条弃权——
        「后果」在本文可能有两节，那时表达本身就不足以定位（ADR-0027 决策 8 的守卫一）。
        """

        wanted = _normalize_heading_name(name)
        if not wanted:
            return None
        matches = [
            record
            for record in self.of_document(document_id)
            if _normalize_heading_name(record.heading_title).startswith(wanted)
        ]
        if len(matches) != 1:
            return None
        return matches[0]

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


#: 标题自带的编号，五种惯例：「``3.``」/「``决策五``」/「``第 2 条``」/「``第 2 章``」/「``Section 2``」。
_TITLE_ARABIC_PREFIX_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)*)(?![0-9])")
_TITLE_CLAUSE_PREFIX_RE = re.compile(r"^第\s*([0-9０-９]+|[一二三四五六七八九十百]+)\s*[条节款项]")
_TITLE_DECISION_PREFIX_RE = re.compile(r"^决策\s*([0-9０-９]+|[一二三四五六七八九十百]+)")
_TITLE_ENGLISH_PREFIX_RE = re.compile(r"^section\s+([0-9０-９]+)\b", re.IGNORECASE)
_CHAPTER_PREFIX_RE = re.compile(r"^第\s*([0-9０-９]+|[一二三四五六七八九十百]+)\s*章")


def _heading_number_of(title: str) -> str | None:
    """标题开头的编号（归一成阿拉伯串）；认不出回 ``None``。

    **``2.4 抵扣上限`` 的编号是 ``2.4``，不是 ``2``**——这也是「`2` 不许匹配 `2.1`」那条
    判据的另一半：多段编号整体保留，单段比较时自然对不上。
    """

    plain = title.strip()
    for pattern in (
        _TITLE_ARABIC_PREFIX_RE,
        _TITLE_CLAUSE_PREFIX_RE,
        _TITLE_DECISION_PREFIX_RE,
        _TITLE_ENGLISH_PREFIX_RE,
    ):
        match = pattern.match(plain)
        if match:
            return to_arabic_number(match.group(1))
    return None


def _chapter_number_of(segment: str) -> str | None:
    """标题路径里某一段的章编号（``第三章 退款与终止`` → ``3``）。"""

    match = _CHAPTER_PREFIX_RE.match(segment.strip())
    if match:
        return to_arabic_number(match.group(1))
    return None


def _unique_by_number(sections: Sequence[SectionRecord], number: str) -> SectionRecord | None:
    """标题编号恰为 ``number`` 的节；恰好一个才算命中（`2` 不匹配 `2.1`，见上）。"""

    matches = [
        record
        for record in sections
        if _number_prefix_re(number).match(record.heading_title.strip())
        or _heading_number_of(record.heading_title) == number
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _has_clause_heading(sections: Sequence[SectionRecord], number: str) -> bool:
    return any(_heading_number_of(record.heading_title) == number for record in sections)


def _normalize_anchor(text: str) -> str:
    return _ANCHOR_NOISE_RE.sub("", text.lower())


def to_arabic_number(token: str) -> str | None:
    """``３`` / ``三`` / ``0013`` / ``2.4`` → 阿拉伯数字串；认不出回 ``None``（不猜）。

    「编号比较」两边都走它：全角与中文数字不是不同的编号，只是不同的写法。多段编号
    （``2.4``）原样保留——**``2`` 与 ``2.4`` 是两个不同的节号**，比较时不许折叠。
    """

    plain = token.translate(_FULLWIDTH_DIGITS)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", plain):
        return ".".join(str(int(part)) for part in plain.split("."))
    value = chinese_number_to_int(plain)
    return str(value) if value is not None else None


def _arabicize_chinese_numbers(text: str) -> str:
    """把文本里的中文数字段统一成阿拉伯（归一化的一步，两边都走同一个函数）。"""

    def replace(match: re.Match[str]) -> str:
        value = chinese_number_to_int(match.group(0))
        return str(value) if value is not None else match.group(0)

    return _CN_NUMERAL_RUN_RE.sub(replace, text)


def _normalize_heading_name(text: str) -> str:
    """标题名比较前的归一化：去标点 / 全角→半角 / 中文数字→阿拉伯 / 小写。"""

    text = text.translate(_FULLWIDTH_DIGITS)
    text = _arabicize_chinese_numbers(text)
    return _HEADING_NAME_NOISE_RE.sub("", text.lower())


#: 文档编号（`0013. …` / `0013-collapse-to-sections.md` / `ADR-0013`）→ 统一的数字串。
_DOCUMENT_CODE_RE = re.compile(r"^(?:ADR[-\s_]*)?([0-9]{1,4})(?:[.\-—、\s]|$)", re.IGNORECASE)


def _document_code_of(record: "SectionRecord") -> str | None:
    """这份文档的编号（标题开头的 ``0013.`` 或文件名主干 ``0013-…``），没有就 ``None``。"""

    for text in (record.document_title.strip(), _link_stem(record.source_filename)):
        if not text:
            continue
        match = _DOCUMENT_CODE_RE.match(text)
        if match:
            return match.group(1).lstrip("0") or "0"
    return None



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

    ``kind`` 决定「目标怎么找」：``title`` 按标题（书名号里写的就是标题）、``link`` 按
    链接目标（**文件名优先**，标题兜底）、``same_document`` 只在本篇里找、
    ``heading_name`` 按标题名（R1：引号短语 / 裸标题名）、``decision`` 按编号（R2）。

    R1 / R2 的定位符各有自己的字段：``heading_name`` 是标题名原文，``decision_number``
    是「决策 N」里的 N，``chapter_number`` + ``section_number`` 才是「第 M 章第 N 节」；
    ``document_code`` 是 ``ADR-0013`` 这类文档编号（目标文档由它定，不是标题名定）。
    """

    kind: str
    expression: str
    sentence: str
    start: int
    end: int
    document_title: str | None = None
    section_number: str | None = None
    anchor: str | None = None
    link_target: str | None = None
    heading_name: str | None = None
    decision_number: str | None = None
    chapter_number: str | None = None
    document_code: str | None = None


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


def extract_reference_candidates(
    text: str, *, known_titles: Sequence[str] = ()
) -> tuple[ReferenceCandidate, ...]:
    """从一段正文里认出所有**显式引用表达**（按出现顺序）。

    五类，都是「文档作者亲自指的线」：

    * ``《标题》第 N 节`` / ``《标题》2.4 节`` —— 跨文档（按**标题**找目标）；
    * ``本文件第 N 节`` / ``本文第三节`` —— 同文档（必须是「本文件」这类限定语）；
    * ``[文本](#锚点)`` —— 同文档锚点；
    * ``[文本](文件名.md)`` / ``[文本](文件名.md#锚点)`` / ``[文本](文件名.md)第 N 节`` /
      ``[文本](文件名.md)「节标题」`` —— 跨文档链接（**先按文件名找目标，再回落标题**）；
    * **R1 标题名**（ADR-0027 决策 8）：``「后果」`` / ``见翻案条件``。引号短语要有「本文 /
      文末 / 文档编号 / 链接」给出上下文；裸标题名要**紧跟引用动词**，且只有 ``known_titles``
      里有（≥4 字）的才算——两字标题（``背景``）在正文里到处都是，撞上的概率是必然。

    裸的「第 3 节」**不在这里**：它既可能指本文也可能指别处，属于歧义。

    **链接内部的文字不再单独认一次**：``[详见《X》第 2 节](x.md)`` 里的书名号表达被链接
    覆盖，只留链接那一条候选——否则同一句会产出两条指向可能不同目标的边。
    """

    if not text:
        return ()
    candidates: list[ReferenceCandidate] = []
    for match in _TITLED_REFERENCE_RE.finditer(text):
        groups = match.groupdict()
        candidates.append(
            ReferenceCandidate(
                kind="title",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                document_title=match.group("title").strip(),
                chapter_number=groups.get("chapter"),
                section_number=groups.get("nested") or groups.get("number"),
            )
        )
    for match in _SAME_DOCUMENT_REFERENCE_RE.finditer(text):
        groups = match.groupdict()
        candidates.append(
            ReferenceCandidate(
                kind="same_document",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                chapter_number=groups.get("chapter"),
                section_number=groups.get("nested") or groups.get("number"),
            )
        )
    candidates.extend(_decision_candidates(text))
    candidates.extend(_english_section_candidates(text))
    candidates.extend(_bare_chapter_section_candidates(text))
    links = _link_candidates(text)
    candidates.extend(_quoted_heading_candidates(text))
    candidates.extend(_bare_heading_candidates(text, known_titles))
    # 被链接覆盖的书名号 / 同文档 / 引号标题名丢掉（见函数文档最后一段）。
    kept = [
        candidate
        for candidate in candidates
        if not any(link.start <= candidate.start and candidate.end <= link.end for link in links)
    ]
    # 同一处被两条规则重复认出（例如引号短语同时被裸名规则命中）时只留一条。
    deduped: list[ReferenceCandidate] = []
    seen: set[tuple[int, int, str]] = set()
    for candidate in sorted(kept, key=lambda item: (item.start, -item.end)):
        key = (candidate.start, candidate.end, candidate.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    deduped.extend(links)
    deduped.sort(key=lambda candidate: candidate.start)
    return tuple(deduped)


#: 书名号标题（引号标题名的「《标题》」上下文）。
_BOOK_TITLE_RE = re.compile(r"《([^》\n]{1,80})》")

#: 紧贴引号标题名之前的链接（``[ADR-0010](x.md)「第三次修订」``）。
_TRAILING_LINK_RE = re.compile(r"\]\(([^)\s]+)\)\s*$")

#: 裸标题名之前的引用动词（`见翻案条件`）。
_CITATION_VERB_BEFORE_RE = re.compile(rf"(?:{'|'.join(_CITATION_VERBS)})\s*$")


def _link_candidates(text: str) -> list[ReferenceCandidate]:
    """全部 Markdown 链接候选；链接后面紧跟的定位符属于这条链接。"""

    links: list[ReferenceCandidate] = []
    for match in _MARKDOWN_LINK_RE.finditer(text):
        target = match.group("target").strip()
        path, _, anchor = target.partition("#")
        if not path:
            if not anchor:
                continue
            links.append(
                ReferenceCandidate(
                    kind="same_document",
                    expression=match.group(0),
                    sentence=_sentence_around(text, match.start(), match.end()),
                    start=match.start(),
                    end=match.end(),
                    anchor=anchor,
                )
            )
            continue
        stem = _link_stem(path)
        if not stem:
            continue
        # 链接后面紧跟的定位符属于这条链接（语料里的写法就是
        # ``见[《积分规则》](./points_policy.md)第 3 节``）：不吃进来，链接只能指到整篇，
        # 而节点是节——那条边会以「整篇有多节」为由被丢掉。
        trailing = _LINK_LOCATOR_RE.match(text[match.end():])
        end = match.end() + trailing.end() if trailing is not None else match.end()
        heading_name: str | None = None
        section_number: str | None = None
        decision_number: str | None = None
        chapter_number: str | None = None
        if trailing is not None:
            groups = trailing.groupdict()
            if groups.get("quoted") is not None:
                heading_name = groups["quoted"]
            elif groups.get("decision") is not None:
                decision_number = groups["decision"]
            elif groups.get("chapter") is not None:
                chapter_number = groups["chapter"]
                section_number = groups.get("nested")
            elif groups.get("number") is not None:
                section_number = groups["number"]
        links.append(
            ReferenceCandidate(
                kind="link",
                expression=text[match.start():end],
                sentence=_sentence_around(text, match.start(), end),
                start=match.start(),
                end=end,
                anchor=anchor or None,
                link_target=stem,
                heading_name=heading_name,
                section_number=section_number,
                decision_number=decision_number,
                chapter_number=chapter_number,
            )
        )
    return links


def _heading_name_context(text: str, start: int, end: int) -> tuple[str, str | None] | None:
    """引号标题名的上下文：它指向**哪一篇**（文档编号 / 《标题》 / 链接 / 本文）。

    没有上下文时返回 ``None``：光有引号只说明「在引用原文」，不说明「指向哪一节」——
    这类短语不建边（精度守卫）。
    """

    window = text[max(0, start - 60):start]
    document: tuple[int, str, str | None] | None = None
    for match in _ADR_CODE_RE.finditer(window):
        if document is None or match.end() > document[0]:
            document = (match.end(), "code", match.group(1))
    for match in _BOOK_TITLE_RE.finditer(window):
        if document is None or match.end() > document[0]:
            document = (match.end(), "title", match.group(1).strip())
    trailing = _TRAILING_LINK_RE.search(window)
    if trailing is not None:
        path, _, _ = trailing.group(1).partition("#")
        stem = _link_stem(path)
        if stem and (document is None or len(window) > document[0]):
            document = (len(window), "link", stem)
    if document is not None:
        return document[1], document[2]
    if _SAME_DOCUMENT_MARKERS.search(window):
        return "same_document", None
    if _QUOTED_SUFFIX_MARKER_RE.match(text[end:end + 6]):
        return "same_document", None
    return None


def _quoted_heading_candidates(text: str) -> list[ReferenceCandidate]:
    """R1：引号括住的标题名（``「后果」`` / ``「后续修订（第 7 步）」``）。"""

    candidates: list[ReferenceCandidate] = []
    for match in _QUOTED_HEADING_RE.finditer(text):
        name = match.group(1).strip()
        # 引号里是一整句「格式示例」（含另一个书名号）时不是标题名。
        if not name or "《" in name:
            continue
        context = _heading_name_context(text, match.start(), match.end())
        if context is None:
            continue
        kind, value = context
        document_code: str | None = None
        document_title: str | None = None
        link_target: str | None = None
        if kind == "code":
            document_code = value
        elif kind == "title":
            document_title = value
        elif kind == "link":
            link_target = value
        candidates.append(
            ReferenceCandidate(
                kind="heading_name",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                heading_name=name,
                document_code=document_code,
                document_title=document_title,
                link_target=link_target,
            )
        )
    return candidates


def _bare_heading_candidates(
    text: str, known_titles: Sequence[str]
) -> list[ReferenceCandidate]:
    """R1：紧跟引用动词的裸标题名（``见翻案条件``）。短名（<4 字）不试。"""

    found: dict[tuple[int, int], str] = {}
    for title in known_titles:
        raw = title.strip()
        if len(_normalize_heading_name(raw)) < 4:
            continue
        for match in re.finditer(re.escape(raw), text):
            key = (match.start(), match.end())
            if key in found:
                continue
            before = text[max(0, match.start() - 8):match.start()]
            if not _CITATION_VERB_BEFORE_RE.search(before):
                continue
            found[key] = raw
    return [
        ReferenceCandidate(
            kind="heading_name",
            expression=text[start:end],
            sentence=_sentence_around(text, start, end),
            start=start,
            end=end,
            heading_name=title,
        )
        for (start, end), title in sorted(found.items())
    ]


#: 句子边界（决策号的文档上下文只看同一句里的文档定位符）。
_SENTENCE_BOUNDARIES = "。！？；!?;\n"


def _cross_document_marker(text: str, start: int) -> tuple[str, str] | None:
    """同一句里、决策号之前的文档定位符（``（ADR-0025 决策五）`` 里的 ``ADR-0025``）。"""

    window = text[max(0, start - 80):start]
    left = max((window.rfind(mark) for mark in _SENTENCE_BOUNDARIES), default=-1) + 1
    window = window[left:]
    marker: tuple[int, str, str] | None = None
    for match in _ADR_CODE_RE.finditer(window):
        if marker is None or match.end() > marker[0]:
            marker = (match.end(), "code", match.group(1))
    for match in _BOOK_TITLE_RE.finditer(window):
        if marker is None or match.end() > marker[0]:
            marker = (match.end(), "title", match.group(1).strip())
    trailing = _TRAILING_LINK_RE.search(window)
    if trailing is not None:
        path, _, _ = trailing.group(1).partition("#")
        stem = _link_stem(path)
        if stem and (marker is None or len(window) > marker[0]):
            marker = (len(window), "link", stem)
    if marker is None:
        return None
    return marker[1], marker[2]


def _decision_is_framed_as_citation(text: str, match: re.Match[str]) -> bool:
    """决策号有没有被写成引用框架（``见决策 3`` / ``（决策 5）`` / ``决策 2 说``）。

    没有框架的陈述句（``与决策二（…）直接冲突``）不认：那句话的主语是「决策二」这个
    概念，不是「去看第 2 节」（ADR-0027 决策 8 的精度守卫）。
    """

    before = text[max(0, match.start() - 6):match.start()]
    if _CITATION_VERB_BEFORE_RE.search(before):
        return True
    if before.rstrip().endswith(("（", "(")):
        return True
    after = text[match.end():match.end() + 4]
    return bool(re.match(r"\s*(?:说|里|中)", after))


def _decision_candidates(text: str) -> list[ReferenceCandidate]:
    """R2：``决策 N``。有文档定位符时按那篇文档找；否则只在本篇里找且要引用框架。"""

    candidates: list[ReferenceCandidate] = []
    for match in _DECISION_REFERENCE_RE.finditer(text):
        marker = _cross_document_marker(text, match.start())
        if marker is None and not _decision_is_framed_as_citation(text, match):
            continue
        document_code: str | None = None
        document_title: str | None = None
        link_target: str | None = None
        if marker is not None:
            kind, value = marker
            if kind == "code":
                document_code = value
            elif kind == "title":
                document_title = value
            else:
                link_target = value
        candidates.append(
            ReferenceCandidate(
                kind="decision",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                decision_number=match.group("number"),
                document_code=document_code,
                document_title=document_title,
                link_target=link_target,
            )
        )
    return candidates


def _english_section_candidates(text: str) -> list[ReferenceCandidate]:
    """R2：英文附件的 ``Section N``。``Section N of the X`` 指别的文档，本篇解析不了就弃权。"""

    candidates: list[ReferenceCandidate] = []
    for match in _ENGLISH_SECTION_REFERENCE_RE.finditer(text):
        following = text[match.end():]
        if re.match(r"\s*of\s+(?:the|this|that)\b", following, re.IGNORECASE):
            # 「Section 1 of the SLA terms」：定位符指向另一份文档，本目录解析不了。
            continue
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
    return candidates


#: 「刚说完限定语 / 书名号标题 / 链接」的判据（裸章-节只在没有它们时才补候选）。
_DOCUMENT_QUALIFIER_BEFORE_RE = re.compile(rf"(?:{_DOCUMENT_QUALIFIERS})(?:的)?\s*$")


def _bare_chapter_section_candidates(text: str) -> list[ReferenceCandidate]:
    """R2：裸 ``第 M 章第 N 节``（``详见第三章第二节``）。被限定语 / 书名号 / 链接覆盖时不重复认。"""

    candidates: list[ReferenceCandidate] = []
    for match in _BARE_CHAPTER_SECTION_RE.finditer(text):
        before = text[max(0, match.start() - 12):match.start()]
        if _DOCUMENT_QUALIFIER_BEFORE_RE.search(before):
            continue
        stripped = before.rstrip()
        if stripped.endswith("》"):
            continue
        if stripped.endswith(")") and _TRAILING_LINK_RE.search(before):
            continue
        candidates.append(
            ReferenceCandidate(
                kind="same_document",
                expression=match.group(0),
                sentence=_sentence_around(text, match.start(), match.end()),
                start=match.start(),
                end=match.end(),
                chapter_number=match.group("chapter"),
                section_number=match.group("nested"),
            )
        )
    return candidates





def _link_stem(path: str) -> str:
    """链接目标 / 文件名 → 用来比较的主干：取最后一段、去掉扩展名、小写。

    ``./points_policy.md`` → ``points_policy``；``Points_Policy.MD`` → ``points_policy``；
    ``refund_policy`` → ``refund_policy``。比较两边（链接与 ``source_filename``）都走这一个
    函数，于是「带不带 ``./``」「大小写」「带不带扩展名」都不会让同一条链接对不上。
    """

    name = path.strip().rstrip("/").split("/")[-1].strip()
    if not name:
        return ""
    stem, dot, extension = name.rpartition(".")
    if dot and extension and len(extension) <= 5 and not stem.isdigit():
        return stem.lower()
    return name.lower()


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


def reference_spans(
    text: str, *, known_titles: Sequence[str] = ()
) -> tuple[tuple[int, int], ...]:
    """正文里全部引用表达覆盖的区间（合并重叠），供路标判定删用。

    ``known_titles`` 与 :func:`extract_reference_candidates` 同一份口径：R1 的**裸标题名**
    先要知道本文有哪些节标题才试（两字标题不试）。给了它，``见决策 3`` 与 ``见翻案条件``
    这类新形态才会被算进「引用表达」——否则「纯路标片」会漏成普通切片。
    """

    spans = [
        (candidate.start, candidate.end)
        for candidate in extract_reference_candidates(text, known_titles=known_titles)
    ]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    return tuple(merged)


def is_reference_pointer(text: str, *, known_titles: Sequence[str] = ()) -> bool:
    """这一片是不是**纯路标**（除了引用之外没有实质内容）。

    判据是「删掉全部显式引用表达之后还剩多少可读字符」：少于
    :data:`POINTER_RESIDUAL_CHARS` 个就算。它不猜语义、不看长度、不看是否列表——
    「详见《X》2.4 节。」剩 2 个字（详见）→ 路标；「退款超期需人工审核，详见《X》2.4 节。」
    剩 9 个字 → 不是路标（它自己就回答得了问题）。没有任何引用表达时一律不是路标。
    """

    if not text or not text.strip():
        return False
    spans = reference_spans(text, known_titles=known_titles)
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
    known_titles = _own_heading_titles(own_sections)
    for entry in sections:
        record = entry.record
        if record.document_id != document_id or record.scope != scope:
            continue
        for candidate in extract_reference_candidates(entry.text, known_titles=known_titles):
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


def _own_heading_titles(sections: Sequence[SectionRecord]) -> tuple[str, ...]:
    """本文档全部节标题（R1 裸标题名要在里面找；去重、保序）。"""

    seen: list[str] = []
    for record in sections:
        title = record.heading_title
        if title and title not in seen:
            seen.append(title)
    return tuple(seen)


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
    elif candidate.kind == "link":
        # 链接目标先按**文件名**（source_filename）找文档，找不到再回落标题。
        matched = catalog.documents_for_link_target(candidate.link_target or "")
        if len(matched) != 1:
            return None
        target_document = matched[0]
    elif candidate.document_code is not None:
        # 文档编号（ADR-0013 → 「0013. …」那篇）：技术文档里指另一篇的常见写法。
        matched_by_code = catalog.documents_with_code(candidate.document_code)
        if len(matched_by_code) != 1:
            return None
        target_document = matched_by_code[0]
    elif candidate.document_title is not None:
        titled = catalog.documents_titled(candidate.document_title)
        if len(titled) != 1:
            return None
        target_document = titled[0]
    else:
        # 引号标题名 / 裸标题名没有文档定位符时只在本篇里找。
        target_document = document_id

    target: SectionRecord | None = None
    if candidate.anchor is not None:
        target = catalog.section_by_anchor(document_id=target_document, anchor=candidate.anchor)
    located = _locator_target(candidate, document_id=target_document, catalog=catalog)
    if target is not None and located is not None and target.section_id != located.section_id:
        # 锚点与定位符指向两节：这份表达自相矛盾，不猜。
        return None
    target = target or located
    if target is None and candidate.kind in {"title", "link"} and not _has_locator(candidate):
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


def _has_locator(candidate: ReferenceCandidate) -> bool:
    """这条候选带没带「指到哪一节」的定位符（R1 标题名 / R2 编号都算）。"""

    return any(
        value is not None
        for value in (
            candidate.section_number,
            candidate.heading_name,
            candidate.decision_number,
            candidate.chapter_number,
        )
    )


def _locator_target(
    candidate: ReferenceCandidate, *, document_id: str, catalog: SectionCatalog
) -> SectionRecord | None:
    """按候选的定位符找目标节；**对上零个或两个以上都回 ``None``**（歧义不建边）。"""

    if candidate.heading_name is not None:
        return catalog.section_by_heading_name(
            document_id=document_id, name=candidate.heading_name
        )
    if candidate.decision_number is not None:
        return catalog.section_by_heading_number(
            document_id=document_id, number=candidate.decision_number
        )
    if candidate.chapter_number is not None:
        if candidate.section_number is None:
            return None
        return catalog.section_by_chapter_and_section(
            document_id=document_id,
            chapter=candidate.chapter_number,
            section=candidate.section_number,
        )
    if candidate.section_number is not None:
        return catalog.section_by_number(
            document_id=document_id, number=candidate.section_number
        )
    return None



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
