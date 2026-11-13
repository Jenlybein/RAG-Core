"""引用块：Finalizer 的结构化输出、四条硬校验、渲染串与三态完整度（第 11 步票 #149）。

ADR-0026 是这一块的**权威边界**：受控 Research 只生成「引用块」，不生成「答案」。

```text
最终材料池（E1、E2… 按材料顺序编号）
   │  模型只点下标（核心片 / 辅助片），正文由后端按序号回查材料池
   ▼
引用块 = 文本 + 状态（supported / insufficient）+ 证据下标
   │  四条硬校验：任一不过 → 调用方把 answer 落成 null（材料与计划照给）
   ▼
顶层 answer = 块的渲染串（不是第二种产物）
```

**为什么 ``answer`` 只能是渲染串**：如果它能承载独立内容，校验就要做两遍，而模型写
``answer`` 的那一遍**没有任何下标可核**。把它定成纯函数之后，「对外那串字」与「被校验过的
块」在结构上是同一件东西——这一条同时是对外契约与可测性。

**四条硬校验**（每条都对应一种能被手写出来的模型输出）：

| 校验 | 不过的形状 | 拒绝码 |
| --- | --- | --- |
| 有结论的块必须有核心片 | ``supported`` 但 ``primary`` 为空（只引辅助片 / 一条都不引） | :data:`REJECTED_ASSERTION_WITHOUT_CORE` |
| 材料不足的块不许伪造引用 | ``insufficient`` 却挂着任何下标 | :data:`REJECTED_CITATION_ON_INSUFFICIENT` |
| 不许出现未解析的编号或内部标识 | 下标越界、正文里漏出 ``chunk-…`` / ``section-…`` / ``doc-…``、自己写了 ``E9`` 或「候选 3」 | :data:`REJECTED_UNRESOLVED_REFERENCE` |
| 冲突时分别保留来源、不替用户裁决 | 把两种说法并进一块（``conflict``），或在多来源块里写「以…为准」/「更权威」 | :data:`REJECTED_CONFLICT_ADJUDICATED` |

**拒绝码是一因一码的固定串**：它进日志与内部结果，**不进响应**——响应里那一格只有
``answer = null``。调用方要的是「这次不生成」，不是「后端替你判了哪一条」。

**三态完整度由核心片数算出来**，与切片编排同一口径（``app.services.orchestration`` 的
``complete`` / ``partial`` / ``missing``）：所有块都有核心片 = ``complete``，部分 = ``partial``，
一条都没有 = ``missing``。模型的自评根本没进这里，所以「模型说齐了」在结构上无法影响它。

**这里是纯函数**：不碰库、不读配置、不调模型。模型调用与降级顺序在
``app.services.research``（状态机的第 ⑤ 步），本模块只回答「这份输出能不能对外」。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

#: 三态完整度与切片编排**共用同一份常量**：两个模块各写一份会漂移，而「材料够不够」
#: 在两条链路上必须读同一套词。
from app.services.orchestration import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
)

STATUS_SUPPORTED: Final[str] = "supported"
STATUS_INSUFFICIENT: Final[str] = "insufficient"

#: 拒绝码（一因一码，固定串，不拼异常文本）。它们只进日志与内部结果。
REJECTED_ASSERTION_WITHOUT_CORE: Final[str] = "assertion without a core citation"
REJECTED_CITATION_ON_INSUFFICIENT: Final[str] = "citation on an insufficient block"
REJECTED_UNRESOLVED_REFERENCE: Final[str] = "unresolved evidence reference"
REJECTED_CONFLICT_ADJUDICATED: Final[str] = "conflict adjudicated instead of kept apart"
REJECTED_EMPTY_BLOCK: Final[str] = "no usable answer block"
REJECTED_MALFORMED: Final[str] = "malformed answer output"

#: 最终材料进提示词的字符预算。它**不是**六条硬上限之一：材料可以截断（成本），交回给
#: 调用方的材料不许截断（``materials`` 里逐字完整）。
MAX_FINALIZER_MATERIAL_CHARS: Final[int] = 12000
MAX_FINALIZER_QUERY_CHARS: Final[int] = 500

#: 裁决词表：出现它们意味着这块在替用户决定「哪一份更权威」。只在**多来源**块上判——
#: 单来源材料里「以银行实际到账时间为准」是原文措辞，不是裁决。
_ADJUDICATION_MARKERS: Final[tuple[str, ...]] = ("更权威", "更为权威", "为准", "采信")

#: 内部标识的通用形状（切片 / 节 / 文档 id）：模型自己编一个也得被拦住，所以除了逐条比对
#: 材料池里的 id，还要认这个前缀形状。
_INTERNAL_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:chunk|section|document|doc)[-_][0-9A-Za-z]{3,}"
)
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
#: 正文里的证据编号。不能用 ``\b``：中文字符在 Python 正则里也算 ``\w``，``见E9`` 里
#: 「E」前面就没有边界，而那种写法恰恰是最想拦住的。
_EVIDENCE_MARK_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z])E(\d+)")
#: 未解析的候选编号：模型照着提示词里的序号写进了对外文本。
_CANDIDATE_MARK_RE: Final[re.Pattern[str]] = re.compile(r"候选\s*\d+|第\s*\d+\s*条材料")

_BLOCK_KINDS: Final[tuple[str, ...]] = (STATUS_SUPPORTED, STATUS_INSUFFICIENT)


# ─────────────────────────────────────────────────────────── 模型的输出（结构化）


class AnswerBlockOut(BaseModel):
    """一个引用块，**模型能表达的全部**：文本 + 状态 + 两类下标 + 一个冲突标记。

    没有 ``chunk_id``、没有 ``section_id``、没有「引用文本」：模型点不到库里的身份，
    于是提示词注入最多让它点错下标（一条真实材料站错位置），塞不进凭空生成的内容。

    ``primary`` = 核心片（能直接回答这一点的材料），``supporting`` = 辅助片（背景与补充）。
    只有核心片能撑起一个 ``supported`` 结论——这就是「只引辅助片就下结论不成立」的结构表达。
    """

    text: str
    kind: Literal["supported", "insufficient"]
    primary: list[int] = Field(default_factory=list)
    supporting: list[int] = Field(default_factory=list)
    #: 「这份材料互相矛盾，我拆不开」。标了就整份答案作废——ADR-0026 要的形状是**两块
    #: 并列、各自带来源**，不是一块把两种说法并在一起（那样调用方拿到的就是一次裁决）。
    conflict: bool = False


class AnswerOut(BaseModel):
    """Finalizer 的顶层输出：只有块，**没有 answer 字段**。"""

    blocks: list[AnswerBlockOut] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────── 交回的形状


class AnswerRejected(Exception):
    """这份输出不过四条硬校验之一：调用方把 ``answer`` 落成 ``null``（材料照给）。

    ``reason`` 是一因一码的固定串（见模块文档的表），只进日志与内部结果。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Citation:
    """一个**已校验**的引用块：文本 + 状态 + 已解析的证据编号（``E1`` 起，升序去重）。"""

    text: str
    status: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class FinalAnswer:
    """一次 Finalizer 的全部产出：渲染串、引用块、三态完整度。三者同源，不可分开构造。"""

    answer: str
    citations: tuple[Citation, ...]
    evidence_state: str


# ─────────────────────────────────────────────────────────── 编号与渲染


def evidence_id(order: int) -> str:
    """材料池第 ``order`` 条（1-based）的对外编号：``E1``、``E2``…

    编号属于**材料池那一格**，不属于块的出现顺序：同一次调用内不复用、不重排，于是调用方
    可以在自己的答复里直接引用「E3」（ADR-0026 §2 的稳定性契约）。
    """

    return f"E{order}"


def render_answer(citations: Sequence[Citation]) -> str:
    """把引用块按顺序拼成对外那一串字。**``answer`` 没有第二种来源。**

    有结论的块带上它解析到的证据编号（``（E1、E3）``），材料不足的块明说「材料不足」——
    调用方读一段纯文本时也能看出哪一部分没有依据。
    """

    lines: list[str] = []
    for citation in citations:
        if citation.status == STATUS_SUPPORTED and citation.evidence_ids:
            lines.append(f"{citation.text}（{'、'.join(citation.evidence_ids)}）")
        else:
            lines.append(f"{citation.text}（材料不足）")
    return "\n".join(lines)


def evidence_state_of(*, citations: Sequence[Citation], materials: Sequence[Mapping[str, Any]]) -> str:
    """三态完整度：**由核心片数算出来**，与切片编排同一口径（不看模型自评）。

    ``complete`` = 每一块都有核心片；``partial`` = 部分块有；``missing`` = 一条都没有
    （材料池空、全是 ``insufficient``、或压根没有通过校验的块，都属于最后这一档）。
    """

    if not materials or not citations:
        return COMPLETENESS_MISSING
    supported = sum(1 for citation in citations if citation.status == STATUS_SUPPORTED)
    if supported == len(citations):
        return COMPLETENESS_COMPLETE
    if supported == 0:
        return COMPLETENESS_MISSING
    return COMPLETENESS_PARTIAL


# ─────────────────────────────────────────────────────────── 提示词


def build_finalizer_prompt(
    *, query: str, materials: Sequence[Mapping[str, Any]]
) -> str:
    """Finalizer 的用户消息：问题 + 最终材料池（序号即引用下标）。

    材料**带序号、不带任何 id**：模型唯一的指针是「第几条」。正文按预算截断，但截断只
    影响这一次提示词，交回给调用方的材料逐字完整（「成本可以截断，材料不许截断」）。
    """

    lines: list[str] = []
    budget = MAX_FINALIZER_MATERIAL_CHARS
    for index, row in enumerate(materials, start=1):
        if budget <= 0:
            break
        content = str(row.get("content") or "")
        excerpt = content[:budget]
        budget -= len(excerpt)
        title = str(row.get("title") or "")
        heading = str(row.get("heading_path") or "")
        lines.append(
            f"{index}. 《{title}》{f' · {heading}' if heading else ''}\n{excerpt}"
        )
    return (
        f"问题：{query[:MAX_FINALIZER_QUERY_CHARS]}\n\n"
        f"最终材料（共 {len(materials)} 条，序号即引用下标）：\n" + "\n\n".join(lines)
    )


FINALIZER_SYSTEM: Final[str] = (
    "你在为一次知识库检索写引用块。给定用户的问题与最终材料（每条带序号），"
    "针对问题需要的每个要点写一块：文本一句不超过 60 字的中文，只陈述材料里写明的事实；"
    "再给出状态与材料序号。"
    "状态 supported = 材料直接回答了这一点，此时**必须**至少给一条核心片 primary；"
    "状态 insufficient = 材料不足（没有材料能回答这一点），此时 primary 与 supporting 都留空，"
    "文本要明说「未找到」或「材料不足」。"
    "supporting 只能补充背景，不能单独支撑结论。"
    "要求：只输出块的文本、状态与材料序号；不要生成引用文本，不要输出切片 id、文档 id、"
    "章节 id，也不要自己写「E1」这类编号（编号由后端按序号生成）。"
    "两份文档对同一事实说法不一致时：拆成两块，各自只引用自己那份来源；如果你无法拆分，"
    "就把该块的 conflict 标为 true（后端会因此不生成答案、把材料交回调用方）。"
    "**绝不**自己裁决哪一份更权威——不要写「以…为准」「更权威」「建议采信」这类判断。"
)


# ─────────────────────────────────────────────────────────── 校验与装配


def finalize_answer(raw: Any, *, materials: Sequence[Mapping[str, Any]]) -> FinalAnswer:
    """把模型的块输出校验成 :class:`FinalAnswer`；任一硬校验不过就抛 :class:`AnswerRejected`。

    这是本模块唯一的入口：结构校验、下标解析、渲染串与三态完整度**一起**产出，调用方
    拿不到「形状对但没校验」的半成品。
    """

    blocks = _coerce_blocks(raw)
    if not blocks:
        raise AnswerRejected(REJECTED_EMPTY_BLOCK)
    citations = tuple(_validate(block, materials=materials) for block in blocks)
    answer = render_answer(citations)
    _scan_answer(answer, citations=citations, materials=materials)
    return FinalAnswer(
        answer=answer,
        citations=citations,
        evidence_state=evidence_state_of(citations=citations, materials=materials),
    )


def _coerce_blocks(raw: Any) -> list[AnswerBlockOut]:
    """收下 ``AnswerOut`` / ``dict`` 两种形状（Provider 与用例替身各用其中一种）。"""

    items = raw.get("blocks") if isinstance(raw, dict) else getattr(raw, "blocks", None)
    if not isinstance(items, list):
        raise AnswerRejected(REJECTED_MALFORMED)

    blocks: list[AnswerBlockOut] = []
    for item in items:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            raise AnswerRejected(REJECTED_MALFORMED)
        text = item.get("text")
        kind = item.get("kind")
        if not isinstance(text, str) or not text.strip():
            raise AnswerRejected(REJECTED_EMPTY_BLOCK)
        if kind not in _BLOCK_KINDS:
            raise AnswerRejected(REJECTED_MALFORMED)
        blocks.append(
            AnswerBlockOut(
                text=text.strip(),
                kind=kind,
                primary=_indices(item.get("primary")),
                supporting=_indices(item.get("supporting")),
                conflict=bool(item.get("conflict")),
            )
        )
    return blocks


def _indices(value: Any) -> list[int]:
    """下标列表：只认真正的整数（``bool`` 是 ``int`` 的子类，得单独挡掉）。"""

    if value is None:
        return []
    if not isinstance(value, list):
        raise AnswerRejected(REJECTED_MALFORMED)
    indices: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise AnswerRejected(REJECTED_MALFORMED)
        indices.append(item)
    return indices


def _validate(block: AnswerBlockOut, *, materials: Sequence[Mapping[str, Any]]) -> Citation:
    """一块的完整校验：状态与下标必须互相成立，否则抛对应的拒绝码。"""

    for index in (*block.primary, *block.supporting):
        if not 1 <= index <= len(materials):
            # 越界下标不是材料（不猜、不截、不用模型给过的任何文本补位）。
            raise AnswerRejected(REJECTED_UNRESOLVED_REFERENCE)

    if block.kind == STATUS_INSUFFICIENT and (block.primary or block.supporting):
        raise AnswerRejected(REJECTED_CITATION_ON_INSUFFICIENT)
    if block.kind == STATUS_SUPPORTED and not block.primary:
        raise AnswerRejected(REJECTED_ASSERTION_WITHOUT_CORE)
    if block.conflict:
        # 「拆不开」= 把两种说法并进一块：ADR-0026 要的是两块并列。
        raise AnswerRejected(REJECTED_CONFLICT_ADJUDICATED)

    cited = set(block.primary) | set(block.supporting)
    if len({_document_of(materials[index - 1]) for index in cited}) > 1 and _adjudicates(
        block.text
    ):
        raise AnswerRejected(REJECTED_CONFLICT_ADJUDICATED)

    return Citation(
        text=block.text,
        status=block.kind,
        evidence_ids=tuple(evidence_id(index) for index in sorted(cited)),
    )


def _document_of(row: Mapping[str, Any]) -> str:
    """一条材料属于哪篇文档：冲突判定的单位是文档，不是节。"""

    return str(row.get("document_id") or "")


def _adjudicates(text: str) -> bool:
    """这块文本有没有替用户裁决「哪一份更权威」。"""

    return any(marker in text for marker in _ADJUDICATION_MARKERS)


def _scan_answer(
    answer: str,
    *,
    citations: Sequence[Citation],
    materials: Sequence[Mapping[str, Any]],
) -> None:
    """对外文本里不许出现内部标识，也不许出现没被这一份答案解析过的编号。

    「答案里」= 渲染串本身：它包含模型写的文本与后端拼上的编号，所以一处扫完就够——
    分两处扫会出现「文本干净、渲染串脏」这种漏网。
    """

    for row in materials:
        for key in ("chunk_id", "section_id", "document_id"):
            value = str(row.get(key) or "")
            if len(value) >= 4 and value in answer:
                raise AnswerRejected(REJECTED_UNRESOLVED_REFERENCE)
    if _INTERNAL_ID_RE.search(answer) or _UUID_RE.search(answer):
        raise AnswerRejected(REJECTED_UNRESOLVED_REFERENCE)

    resolved = {evidence for citation in citations for evidence in citation.evidence_ids}
    for match in _EVIDENCE_MARK_RE.finditer(answer):
        if f"E{match.group(1)}" not in resolved:
            raise AnswerRejected(REJECTED_UNRESOLVED_REFERENCE)
    if _CANDIDATE_MARK_RE.search(answer):
        raise AnswerRejected(REJECTED_UNRESOLVED_REFERENCE)


__all__ = [
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_MISSING",
    "COMPLETENESS_PARTIAL",
    "FINALIZER_SYSTEM",
    "MAX_FINALIZER_MATERIAL_CHARS",
    "REJECTED_ASSERTION_WITHOUT_CORE",
    "REJECTED_CITATION_ON_INSUFFICIENT",
    "REJECTED_CONFLICT_ADJUDICATED",
    "REJECTED_EMPTY_BLOCK",
    "REJECTED_MALFORMED",
    "REJECTED_UNRESOLVED_REFERENCE",
    "STATUS_INSUFFICIENT",
    "STATUS_SUPPORTED",
    "AnswerBlockOut",
    "AnswerOut",
    "AnswerRejected",
    "Citation",
    "FinalAnswer",
    "build_finalizer_prompt",
    "evidence_id",
    "evidence_state_of",
    "finalize_answer",
    "render_answer",
]
