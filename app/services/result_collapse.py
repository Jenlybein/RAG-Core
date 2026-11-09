"""把命中的小片折成「交付单元」——一条结果代表**一节**，而不是一片。

## 为什么需要折叠

切块越细，检索越准；但一条 145 字的细片喂不饱模型，而且一次检索的 top_k 名额
会被同一节的几个片段占满。折叠做的事就是：**用小片算分，用大块交付，一个父节
只占一个名额。**

## 为什么是纯函数

评测与生产必须用同一份折叠逻辑。此前三次数字出错，根因都是「评测复刻生产逻辑
但复刻错了」。函数不碰数据库、不碰框架，内容由调用方以字典传入——两边共用一份
实现，口径就不可能再错开。

调用方负责把它取来的内容喂进来：

* ``sections``：``{单元键: ((序号, 正文, chunk_id), ...)}``，已按文档顺序排好；
* ``tables``：``{单元键: 整表全文}``。

拿不到内容时回落到命中片自身的正文——**绝不返回空结果**。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, NamedTuple, Sequence

# 块类型（chunk_type）取值见 GLOSSARY 的「块类型」（五种）。折叠只关心一种：
# 行级表片要被折回它所属的整表。其余类型一律按父节归组。
CHUNK_TABLE = "table"
CHUNK_TABLE_ROW = "table_row"

# 单元种类：折叠的粒度由它决定，也是 ``unit_key`` 的第一段。
class UnitKey(NamedTuple):
    """单元键。

    做成命名元组而不是裸元组：``key[2]`` / ``key[-1]`` 在 2/3/4 元组混用时
    会静默取到错的东西，而且读代码时看不出第 3 段到底是文档还是章节。
    它会与等值的普通元组比较相等，所以调用方按元组建索引也仍然对得上。
    """

    kind: str
    document_id: str
    section_id: str
    table_part: str = ""


UNIT_TABLE = "table"
UNIT_SECTION = "section"
UNIT_CHUNK = "chunk"

# 扩展粒度，写给调用方看「这次上下文给了多大范围」。
EXPANSION_TABLE = "table"
EXPANSION_PARENT = "parent"
EXPANSION_NEIGHBORS = "neighbors"
EXPANSION_CHUNK = "chunk"

# 单元键：``(种类, 文档 ID, 节 ID[, table_part])``。用元组而不是字符串，
# 避免把 ID 拼进字符串再猜分隔符。

# 去重后条数会变少，所以召回要多取几倍。因子是常量而不是配置项：15 道题的
# 样本量下，任何可调旋钮都会被评测调成过拟合值。实测每题平均 1.7 片同节命中。
OVERFETCH_FACTOR = 3

# 同节切片的行形态：(序号, 正文, chunk_id)。序号是文档级 chunk_index。
SectionRows = tuple[tuple[int, str, str], ...]


@dataclass(frozen=True)
class Hit:
    """一条命中：折叠的输入。字段就是算分与归组要用的那些。"""

    chunk_id: str
    document_id: str
    score: float
    content: str
    chunk_type: str = ""
    section_id: str = ""
    parent_section_id: str = ""
    table_part: int = 0
    heading_path: str = ""
    kb_id: str | None = None


@dataclass(frozen=True)
class Unit:
    """一个交付单元：折叠的输出，将来直接映射成一条响应结果。"""

    unit_key: UnitKey
    section_id: str
    chunk_id: str
    score: float
    content: str
    source_chunk_ids: tuple[str, ...]
    expansion: str
    heading_path: str = ""
    kb_id: str | None = None

    @property
    def is_table(self) -> bool:
        return self.unit_key[0] == UNIT_TABLE


def unit_path_of(heading_path: str) -> str:
    """把任意深度的章节路径截到**单元层级**：前两段（文档 > 大节）。

    为什么是前两段：折叠的语义是「一条结果代表一节」，而这里的「节」是 h2 级——
    再细就是切片，再粗就跨了内容主题。

    **不能用 `parent_section_id`**（上一级）来代替：对 h2 级的切片（这个节下面
    没有 h3），它的上一级是 h1，按它归组会把整篇文档折成一个单元。这个坑真踩过：
    40 个 h2 小节的文档被折成 1 条结果，交付口径的分数因此虚高。
    """

    segments = [part.strip() for part in heading_path.split(">") if part.strip()]
    return " > ".join(segments[:2])


def unit_id_of(heading_path: str) -> str:
    """单元 id = ``sha256(单元路径)``。

    与 ``section_id`` 同一套算法（见「父块」词条），好处是**不需要新增列**：
    从每个切片已有的 ``heading_path`` 就能算出来，重建索引前后都稳定。
    """

    path = unit_path_of(heading_path)
    return hashlib.sha256(path.encode("utf-8")).hexdigest() if path else ""


def unit_key(hit: Hit, *, merge_table_rows: bool = True) -> UnitKey:
    """算出这条命中属于哪个单元。

    * 行级表片 → 它所属的那张表（同一张表的多行只占一个名额）；
    * 其余 → 它所属的**父节**（``parent_section_id`` 优先，退到 ``section_id``）；
    * 没有章节路径（无标题文档）→ 自己成单元，不造一个假父节。

    ``merge_table_rows=False`` 时行级片各自成单元——留给「只要精确引用」的调用方。
    """

    unit_id = unit_id_of(hit.heading_path)
    if hit.chunk_type == CHUNK_TABLE_ROW:
        if merge_table_rows and unit_id:
            return UnitKey(UNIT_TABLE, hit.document_id, unit_id, str(hit.table_part))
        # 关掉合并时行级片各自成单元——调用方要的是「精确到那一行」的引用，
        # 退回按节归组会让它们又被并成一个名额，那正是它想避免的。
        return UnitKey(UNIT_CHUNK, hit.document_id, hit.chunk_id)
    if unit_id:
        return UnitKey(UNIT_SECTION, hit.document_id, unit_id)
    # 只有真的没有章节路径（无标题文档）才各自成单元。
    return UnitKey(UNIT_CHUNK, hit.document_id, hit.chunk_id)


def collapse(
    hits: Sequence[Hit],
    *,
    sections: Mapping[UnitKey, SectionRows] | None = None,
    tables: Mapping[UnitKey, str] | None = None,
    top_k: int | None = None,
    merge_table_rows: bool = True,
) -> list[Unit]:
    """把命中列表折成交付单元列表。

    前置条件：``hits`` 已按最终排序分**降序**排列。折叠后的顺序取「该单元内最高
    分那片」在列表中的首次出现位置——也就是沿用调用方排好的顺序，不重新排序，
    免得打乱融合层定好的次序（ADR-0010 的确定性要求）。

    跨单元的判定沿用改造前的口径：本次命中落在 ≥2 个单元上时，每个单元都给整节
    ——跨节的题本来就需要多节上下文。
    """

    if not hits:
        return []

    groups: dict[UnitKey, list[Hit]] = {}
    for hit in hits:
        groups.setdefault(unit_key(hit, merge_table_rows=merge_table_rows), []).append(hit)

    cross_unit = len(groups) >= 2
    units: list[Unit] = []
    for key, group in groups.items():
        # 组内代表片 = 分最高那片。hits 已降序，所以组内第一个就是它；
        # 仍然显式取 max，避免调用方违反前置条件时算出奇怪的结果。
        best = max(group, key=lambda item: item.score)
        if key[0] == UNIT_TABLE:
            content = (tables or {}).get(key) or best.content
            expansion = EXPANSION_TABLE
        else:
            content, expansion = _section_content(
                key, group, sections=sections, cross_unit=cross_unit
            )
        units.append(
            Unit(
                unit_key=key,
                section_id=_unit_section_id(key, best),
                chunk_id=best.chunk_id,
                score=best.score,
                content=content,
                source_chunk_ids=tuple(hit.chunk_id for hit in group),
                expansion=expansion,
                heading_path=best.heading_path,
                kb_id=best.kb_id,
            )
        )

    return units[:top_k] if top_k is not None else units


def _unit_section_id(key: UnitKey, best: Hit) -> str:
    """单元的身份。

    表格单元取它的所属章节；节单元取键的最后一段（父节的 id）。

    第三种情况——单片自成单元（无标题文档，或关掉行级片合并）——身份只能是它
    自己所属的那个节；**绝不能把 chunk_id 填进 section_id**，那会让调用方以为
    拿到的是节。真有文档没有章节时返回空串，由响应层落成 null。
    """

    if key.kind in (UNIT_TABLE, UNIT_SECTION):
        return key.section_id
    return best.section_id


def _section_content(
    key: UnitKey,
    group: list[Hit],
    *,
    sections: Mapping[UnitKey, SectionRows] | None,
    cross_unit: bool,
) -> tuple[str, str]:
    """决定这一节给多大范围：整节、邻居窗口，还是只能退回命中片自身。"""

    rows = (sections or {}).get(key)
    if not rows:
        # 拿不到同节内容（比如文档刚被删）时用命中片自身拼。宁可少上下文，
        # 也不能让这条结果变成空——空结果比短结果更糟。
        return "\n\n".join(hit.content for hit in group), EXPANSION_CHUNK

    hit_ids = {hit.chunk_id for hit in group}
    # 同一单元命中 ≥2 片、或本次检索跨了多个单元 → 整节。
    if cross_unit or len(hit_ids) >= 2:
        return "\n\n".join(row[1] for row in rows), EXPANSION_PARENT

    positions = [index for index, (_, _, cid) in enumerate(rows) if cid in hit_ids]
    if not positions:
        return "\n\n".join(row[1] for row in rows), EXPANSION_PARENT
    centre = positions[0]
    window = rows[max(0, centre - 1) : centre + 2]
    return "\n\n".join(row[1] for row in window), EXPANSION_NEIGHBORS
