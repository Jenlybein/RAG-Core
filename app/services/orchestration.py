"""切片编排：把候选切片整理成「要点 → 核心 / 辅助 → 完整度」（第 11 步票 #147）。

```text
候选（折叠之后的交付单元）→ 池子（≤20 / 留 ≥6 个基础召回位 / 正文 ≤12000 字）
   → ① 要点（A1…An）→ ② 每个要点下的角色（只点候选序号）→ 后端按序号回查 → E1…En
```

**为什么只让模型点序号**：它是唯一能把「正文来自库」变成**结构性事实**的做法。模型的输出
里没有可被劫持的自由文本通道——提示词注入最多让模型点错序号，点错的结果是一条真实切片
站错了位置，而不是一段凭空生成的内容。正文因此永远从候选（库里的原文）重建，模型返回的
任何文本一律丢弃。

**两次调用而不是一次**：一次要它同时给「要点」和「角色」，输出里就会混进「要点是否被
覆盖」这类自评，而完整度必须由**核心片数量**数出来（`complete` / `partial` / `missing`）。
拆成两次之后，第二次的输入里只有要点标签与候选序号，模型能表达的东西被压到最小。

**这不是生成答案**（ADR-0001）：块的三个键是完整度、要点清单、材料清单，没有 `answer`，
也没有一句由模型写的正文。

**失败即降级**：模型装不上 / 超时 / 输出不合法 → 不产出这一块，交回一条专码
（``orchestration failed``），检索结果照常返回。与改写、重排同一个处置方式：附加能力
失败不等于本次调用失败。

**候选池的三条配额**（票面数字，不是配置项）：≤20 条；给基础召回留 ≥6 个位置（注入项
最多占 14 个）；正文合计 ≤12000 字符。它们是**成本**约束——池子有多大，提示词就有多长。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.graph_injection import INJECTED_SOURCE as graph_injection_source

logger = structlog.get_logger(__name__)

#: 池子的三条配额（票面常量，**不走环境变量**）：它们决定「一次编排最多烧多少提示词」，
#: 做成可调项会让同一个档位名在不同部署下变成不同成本。
MAX_CANDIDATES = 20
MIN_BASE_CANDIDATES = 6
MAX_PROMPT_CHARS = 12000

#: 要点个数的硬上限。模型多给了就按「输出不合法」处理，而不是悄悄截断：截断之后的要点
#: 清单与模型心里的那份不是同一个东西，而完整度是按要点算的——那会让三态变得不可解释。
MAX_POINTS = 5

#: 每个要点最多几条核心片 / 辅助片：一条要点给 10 条材料等于没分角色。
MAX_ROLES_PER_POINT = 3

#: 降级原因（ADR-0010「第七次修订」里那第五个固定串）。
DEGRADED_REASON = "orchestration failed"

COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_MISSING = "missing"

ROLE_PRIMARY = "primary"
ROLE_SUPPORTING = "supporting"

#: 图谱注入项在候选里带的出处标记（#144 生产、#147 消费）。它只在这里被**读**：
#: 字面量住在 ``app.services.graph_injection``（写的那一侧），两边各写一份必然漂移——
#: 漂移的表现是「注入项被当成基础召回」，6 个保留位悄悄失效而不报错。
INJECTED_SOURCE = graph_injection_source


class PointList(BaseModel):
    """第一次调用的结构化输出：只要点，不要编号、不要正文。"""

    points: list[str] = Field(default_factory=list)


class _PointRoleIn(BaseModel):
    point: int
    primary: list[int] = Field(default_factory=list)
    supporting: list[int] = Field(default_factory=list)


class PointRoles(BaseModel):
    """第二次调用的结构化输出：要点序号 + 候选序号，别的一律不收。"""

    points: list[_PointRoleIn] = Field(default_factory=list)


@dataclass(frozen=True)
class Candidate:
    """池子里的一条候选：**身份 + 库里的原文**，序号是模型唯一的指针。

    ``content`` 是材料正文（逐字来自库）；``excerpt`` 是进提示词的那一段（只受预算约束，
    可能与 ``content`` 不同）。两者分开，是因为「成本可以截断」而「材料不许截断」。
    """

    order: int
    chunk_id: str
    kb_id: str
    document_id: str
    title: str
    heading_path: str | None
    content: str
    excerpt: str
    injected: bool = False


@dataclass(frozen=True)
class PointRole:
    """一个要点下的角色分配（序号都是**池子里的 1-based 位置**）。"""

    point: int
    primary: tuple[int, ...] = ()
    supporting: tuple[int, ...] = ()


@dataclass(frozen=True)
class OrchestrationOutcome:
    """交回两样东西：编排块（可能没有）与降级原因（可能没有）。

    两者互斥：有块就没有原因，有原因就没有块。``block=None`` 且 ``degraded_reason=None``
    的意思是「这次没有可编排的东西」（候选为空），那不是故障。
    """

    block: dict[str, Any] | None = None
    degraded_reason: str | None = None


# ─────────────────────────────────────────────────────────── 候选池


def build_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
) -> list[Candidate]:
    """按三条配额建池，并**逐条**校验租户 / 知识库 / 索引版本。

    校验的意义在 #152 落地之后才完全显现：注入项可以来自别的库（跨库边允许存在），
    但对**这次编排**而言越权的材料一条都不许进池。交付单元自身来自内核、天然在范围内，
    所以这里「字段缺失」按本次请求的事实处理（缺 ``tenant_id`` = 本次租户、缺
    ``index_version`` = 该库这次的生效版本），**不是**放行：一旦候选自带出处，就以出处为准。

    池子按入池顺序重排 ``order``（1-based）：序号只表达「在这一次编排里是第几条」，
    模型因此没有可回显的稳定身份（切片 id 不进提示词）。
    """

    budget = MAX_PROMPT_CHARS
    pool: list[Candidate] = []
    injected_count = 0
    injected_limit = MAX_CANDIDATES - MIN_BASE_CANDIDATES

    for row in rows:
        if len(pool) >= MAX_CANDIDATES:
            break
        if not _in_scope(row, tenant_id=tenant_id, index_versions=index_versions):
            continue
        injected = _is_injected(row)
        if injected:
            # 保留位先给基础召回：注入项只能填剩下的位置，且不能把已有位置挤掉。
            if injected_count >= injected_limit:
                continue
            injected_count += 1
        content = str(row.get("content") or "")
        if budget <= 0:
            break
        excerpt = content[:budget]
        budget -= len(excerpt)
        pool.append(
            Candidate(
                order=len(pool) + 1,
                chunk_id=str(row.get("chunk_id") or ""),
                kb_id=str(row.get("kb_id") or ""),
                document_id=str(row.get("document_id") or ""),
                title=str(row.get("title") or ""),
                heading_path=_optional_str(row.get("heading_path")),
                content=content,
                excerpt=excerpt,
                injected=injected,
            )
        )
    return pool


def _in_scope(row: Mapping[str, Any], *, tenant_id: str, index_versions: Mapping[str, str]) -> bool:
    """租户 / 知识库 / 索引版本三项都必须对得上，且身份字段不许是空的。

    「字段缺失」与「字段不对」是两件事：交付单元没有那几个字段（它本来就不带），
    此时按本次请求的事实处理；而一旦带了值，就以值为准——自带出处的候选是注入项。
    """

    kb_id = row.get("kb_id")
    if not isinstance(kb_id, str) or kb_id not in index_versions:
        return False
    if not str(row.get("chunk_id") or "") or not str(row.get("content") or ""):
        return False
    row_tenant = row.get("tenant_id")
    if row_tenant is not None and row_tenant != tenant_id:
        return False
    row_version = row.get("index_version")
    by_version = index_versions[kb_id]
    return row_version is None or row_version == by_version


def _is_injected(row: Mapping[str, Any]) -> bool:
    return row.get("retrieval_source") == INJECTED_SOURCE


def _optional_str(value: Any) -> str | None:
    text = "" if value is None else str(value)
    return text or None


# ─────────────────────────────────────────────────────────── 提示词


def _render_candidates(candidates: Sequence[Candidate]) -> str:
    """候选的提示词形态：序号 + 标题 + 章节路径 + 摘录。**不出现切片 id 与文档 id。**

    不给 id 是刻意的：模型没有可回显的稳定身份，就只能点序号。
    """

    lines: list[str] = []
    for candidate in candidates:
        heading = candidate.heading_path or ""
        lines.append(
            f"{candidate.order}. 《{candidate.title}》{f' · {heading}' if heading else ''}\n"
            f"{candidate.excerpt}"
        )
    return "\n\n".join(lines)


def build_points_prompt(*, query: str, candidates: Sequence[Candidate]) -> str:
    """第一次调用：只问要点（A1…An 之后由后端编号）。"""

    return (
        f"问题：{query}\n\n候选材料（共 {len(candidates)} 条）：\n{_render_candidates(candidates)}"
    )


def build_roles_prompt(
    *, query: str, points: Sequence[str], candidates: Sequence[Candidate]
) -> str:
    """第二次调用：要点已经定好（A1…An），这次只问角色。"""

    point_lines = "\n".join(f"A{index}. {text}" for index, text in enumerate(points, start=1))
    return (
        f"问题：{query}\n\n"
        f"要点清单：\n{point_lines}\n\n"
        f"候选材料（共 {len(candidates)} 条）：\n{_render_candidates(candidates)}"
    )


_POINTS_SYSTEM = (
    "你在为知识库检索结果整理要点清单。给定用户的问题与若干候选材料（每条带序号），"
    f"列出回答这个问题需要的要点，每个要点一句不超过 30 字的中文，最多 {MAX_POINTS} 个。"
    "要求：只能依据给定材料归纳，不要编造材料里没有的内容；不要生成答案正文；"
    "不要给要点编号，也不要提到材料序号。"
)

_ROLES_SYSTEM = (
    "你在为知识库检索结果划分角色。给定问题、要点清单（A1 起）与候选材料（带序号），"
    "为每个要点挑出核心片与辅助片。核心片 = 能直接回答这个要点的材料；"
    "辅助片 = 提供背景或补充、但没有直接回答这个要点的材料。"
    f"要求：每个要点最多 {MAX_ROLES_PER_POINT} 条核心片、{MAX_ROLES_PER_POINT} 条辅助片；"
    "只输出要点编号与材料序号，不要输出材料正文，不要输出切片 id；"
    "材料里没有对应内容时，把该要点的列表留空。"
)


# ─────────────────────────────────────────────────────────── 输出收敛


class _IllegalOutput(Exception):
    """模型输出不合法（结构不对 / 要点为空 / 超出上限）。**只在本模块内部流转。**"""


def _as_int(value: Any) -> int | None:
    """只认真正的整数：``bool`` 是 ``int`` 的子类，得单独挡掉。"""

    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _coerce_points(raw: Any) -> list[str]:
    """把第一次调用的输出收敛成要点文本；收敛不了就抛 :class:`_IllegalOutput`。"""

    items = getattr(raw, "points", None)
    if items is None and isinstance(raw, dict):
        items = raw.get("points")
    if not isinstance(items, list):
        raise _IllegalOutput("points is not a list")

    points: list[str] = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else item
        if not isinstance(text, str):
            raise _IllegalOutput("a point is not text")
        stripped = text.strip()
        if stripped:
            points.append(stripped)
    if not points:
        raise _IllegalOutput("no usable point")
    if len(points) > MAX_POINTS:
        raise _IllegalOutput("too many points")
    return points


def _coerce_roles(raw: Any) -> list[PointRole]:
    """把第二次调用的输出收敛成角色分配；序号**不在这里**判越界。

    越界序号不是「输出不合法」：它是模型点错了（提示词注入、或它在数别的东西）。
    那一条按票面「整条拒掉、不产生材料」处理——见 :func:`assemble_block`。结构本身不对
    （要点不是整数、序号列表里混进别的东西）才算不合法。
    """

    items = getattr(raw, "points", None)
    if items is None and isinstance(raw, dict):
        items = raw.get("points")
    if not isinstance(items, list):
        raise _IllegalOutput("roles is not a list")

    roles: list[PointRole] = []
    for item in items:
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            raise _IllegalOutput("a role entry is not an object")
        point = _as_int(item.get("point"))
        if point is None:
            raise _IllegalOutput("a role entry has no point number")
        roles.append(
            PointRole(
                point=point,
                primary=_coerce_indices(item.get("primary")),
                supporting=_coerce_indices(item.get("supporting")),
            )
        )
    return roles


def _coerce_indices(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _IllegalOutput("indices is not a list")
    indices: list[int] = []
    for item in value:
        index = _as_int(item)
        if index is None:
            raise _IllegalOutput("an index is not an integer")
        indices.append(index)
    return tuple(indices)


# ─────────────────────────────────────────────────────────── 组装（纯函数）


def assemble_block(
    *,
    points: Sequence[str],
    roles: Sequence[PointRole],
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    """把要点 + 角色装配成交付块：**正文只从候选取，编号按要点顺序分配**。

    三条判定写在这一处，因为它们必须同时成立：

    1. **越界序号整条拒掉。** 序号不在 ``1..len(candidates)`` 就不是材料——不猜、不截、
       不用模型给过的任何文本补位；
    2. **完整度由核心片数出来。** 模型的自评（多返回的键）根本没进这个函数，所以
       「模型说齐了」在结构上无法影响三态；
    3. **E 编号按要点顺序、同一要点内核心片先于辅助片。** 顺序稳定，调用方才敢引用「E3」。
    """

    by_point: dict[int, dict[str, list[int]]] = {}
    for role in roles:
        if not 1 <= role.point <= len(points):
            # 要点序号越界：这一条分配作废（它连要点都对不上，无从归属）。
            continue
        bucket = by_point.setdefault(role.point, {ROLE_PRIMARY: [], ROLE_SUPPORTING: []})
        for role_name, indices in (
            (ROLE_PRIMARY, role.primary),
            (ROLE_SUPPORTING, role.supporting),
        ):
            for index in indices:
                if not 1 <= index <= len(candidates):
                    continue  # 越界序号整条拒掉：不产生材料
                if index in bucket[role_name]:
                    continue  # 同一角色里重复点同一条：只算一条
                if len(bucket[role_name]) >= MAX_ROLES_PER_POINT:
                    continue
                bucket[role_name].append(index)

    evidence: list[dict[str, Any]] = []
    point_rows: list[dict[str, Any]] = []
    for point_index, text in enumerate(points, start=1):
        bucket = by_point.get(point_index, {ROLE_PRIMARY: [], ROLE_SUPPORTING: []})
        for role_name in (ROLE_PRIMARY, ROLE_SUPPORTING):
            for index in bucket[role_name]:
                candidate = candidates[index - 1]
                evidence.append(
                    {
                        "evidence_id": f"E{len(evidence) + 1}",
                        "point_id": f"A{point_index}",
                        "role": role_name,
                        "chunk_id": candidate.chunk_id,
                        "kb_id": candidate.kb_id,
                        "document_id": candidate.document_id,
                        "title": candidate.title,
                        "heading_path": candidate.heading_path,
                        "content": candidate.content,
                    }
                )
        point_rows.append(
            {
                "point_id": f"A{point_index}",
                "text": text,
                "has_primary": bool(bucket[ROLE_PRIMARY]),
            }
        )

    return {
        "completeness": _completeness_of(point_rows),
        "points": point_rows,
        "evidence": evidence,
    }


def _completeness_of(point_rows: Sequence[Mapping[str, Any]]) -> str:
    """三态完整度：所有要点都有核心片 = complete；部分 = partial；都没有 = missing。"""

    covered = sum(1 for row in point_rows if row["has_primary"])
    if covered == len(point_rows):
        return COMPLETENESS_COMPLETE
    if covered == 0:
        return COMPLETENESS_MISSING
    return COMPLETENESS_PARTIAL


# ─────────────────────────────────────────────────────────── 组合根


async def _default_llm_factory() -> Any:
    """惰性 import：编排关着时不为一项没开的能力付 import 代价。

    ``fast`` 是既有约定（改写、重排都用它）：编排要的是「快、稳、便宜」，不是最强。
    """

    from app.providers.llm import get_llm_provider

    return await get_llm_provider("fast")


async def orchestrate_evidence(
    *,
    query: str,
    rows: Sequence[Mapping[str, Any]],
    tenant_id: str,
    index_versions: Mapping[str, str],
    llm_factory: Callable[[], Awaitable[Any]] | None = None,
    timeout_ms: int | None = None,
) -> OrchestrationOutcome:
    """跑一次编排；任何失败都收敛成「没有块 + 一条专码」。

    ``llm_factory`` 是**模型注入缝**（父 spec #138：这个服务只吃「候选 + 模型工厂」两个
    输入）：缺省走 ``get_llm_provider("fast")``，测试与评测换掉它就能完整验行为。

    ``timeout_ms`` 是**整步预算**（两次调用共用）：它是可选附加能力的硬上限，不是
    阶段间检查——超了就把这一块丢掉，检索结果照旧。缺省读部署配置。
    """

    pool = build_candidates(rows, tenant_id=tenant_id, index_versions=index_versions)
    if not pool:
        # 没有可编排的东西不是故障：响应里连这一块都不出现，也不记降级。
        return OrchestrationOutcome()

    budget_ms = settings.ORCHESTRATION_TIMEOUT_MS if timeout_ms is None else timeout_ms
    factory = llm_factory or _default_llm_factory
    started = time.perf_counter()
    try:
        block = await asyncio.wait_for(
            _organize(query=query, pool=pool, llm_factory=factory),
            timeout=budget_ms / 1000,
        )
    except Exception as exc:  # noqa: BLE001 - 附加能力失败即降级，绝不整单失败
        logger.warning(
            "evidence orchestration failed",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            candidates=len(pool),
            # 只记类型：模型返回的文案可能夹带正文。
            error=type(exc).__name__,
        )
        return OrchestrationOutcome(degraded_reason=DEGRADED_REASON)
    return OrchestrationOutcome(block=block)


async def _organize(
    *,
    query: str,
    pool: Sequence[Candidate],
    llm_factory: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    """两次结构化调用：要点 → 角色。任一环节拿不到合法输出就抛出去（由调用方降级）。"""

    provider = await llm_factory()

    raw_points = await provider.chat_structured(
        PointList,
        system=_POINTS_SYSTEM,
        user=build_points_prompt(query=query, candidates=pool),
    )
    points = _coerce_points(raw_points)

    raw_roles = await provider.chat_structured(
        PointRoles,
        system=_ROLES_SYSTEM,
        user=build_roles_prompt(query=query, points=points, candidates=pool),
    )
    roles = _coerce_roles(raw_roles)

    return assemble_block(points=points, roles=roles, candidates=pool)


__all__ = [
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_MISSING",
    "COMPLETENESS_PARTIAL",
    "DEGRADED_REASON",
    "MAX_CANDIDATES",
    "MAX_POINTS",
    "MAX_PROMPT_CHARS",
    "MIN_BASE_CANDIDATES",
    "Candidate",
    "OrchestrationOutcome",
    "PointList",
    "PointRole",
    "PointRoles",
    "ROLE_PRIMARY",
    "ROLE_SUPPORTING",
    "assemble_block",
    "build_candidates",
    "build_points_prompt",
    "build_roles_prompt",
    "orchestrate_evidence",
]
