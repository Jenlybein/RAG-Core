"""候选注入（ADR-0027）：融合之后、截断 TopK / 重排之前，按「节」的关系把邻居拉进候选池。

```text
召回 → 融合 →【注入：种子 ≤10，每种子 ≤2，池 ≤30，score = 0】→ 重排 → TopK → 折叠
```

它补的是「同一节的半份事实」与「文档里那句『详见《X》2.4 节』从不指路」这两条缺口：
命中 2.3 时 2.4 与父节/引用目标会被拉进候选池，但**只拉进来、不裁决**。

## 四条边界（少一条都会长成坏事）

1. **只做候选，不做裁决**（决策 4）：注入项的 ``score`` 恒为 **0**，名次交给重排。
   给分数等于让「有引用关系」冒充「相关」，而重排是整条链路上唯一同时看到 query 与
   正文的环节。0 分表达的是「它进了池，名次完全由重排决定」——与「未命中的那一路是
   ``null`` 而不是 0」同一条纪律：**0 是一个明确的值，不是一个空缺**。
2. **注入的不是整节**：注入的是目标节的**一个代表片**（``chunk_index`` 最小、且不是
   路标片那一张）。整节由折叠展开；先注入整节会让候选池体积失控，也会让同一个节在
   折叠前占掉多个名额。
3. **范围由查询堵死**（决策 5）：目标节必须落在本次请求的 ``kb_ids`` 与版本范围里。
   跨库边**允许存在**（《促销规则》在另一个库是常见真实关系），但注入时按
   ``kb_id → 版本`` 的配对映射过滤——过滤失败的表现必须是「少注入几条」，不是
   「多注入一条别的库的」。服务层再核一遍版本，是第二道门而不是重复劳动：查询写错
   时不报错的那类故障，只有两道条件都独立成立才挡得住。
4. **关系查询失败 = 降级**（决策 6）：跳过注入、主链路照跑、``degraded_reason`` 记一条
   专码。一因一码、**不拼异常文本**——调用方按码做判断，异常文案变了不该影响判断；
   日志里也只记异常类型（文案可能夹带正文）。

## 为什么判据住在服务层、查询住在仓储层

「引用 > 兄弟节 > 父节」的优先级、三条上限、去重与 ``score = 0`` 都是**策略**，它们要能
被纯函数直接测到；而「哪些边算本版本的边」是**范围**，只能有一个答案，所以它在
``app.repositories.section_edge_repository``（与写入侧同一份三列作用域）与
``app.repositories.chunk_repository``（与召回同一条 ``scope_filter``）。

**上限是常量，不走环境变量**：可调旋钮在样本量不足时必然被调成过拟合值（与折叠的
over-fetch ×3 同一条理由，ADR-0013 决策 6）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.section_edge import RELATION_PARENT, RELATION_REFERENCE, RELATION_SIBLING
from app.repositories import chunk_repository, section_edge_repository
from app.utils.retrieval_fields import with_score_columns

logger = structlog.get_logger(__name__)

#: 种子数：只从融合结果的头部往外扩。尾部整体是弱命中，扩出来的是噪声。
MAX_SEEDS: Final[int] = 10
#: 每个种子最多拉几个邻居：一条种子给十条邻居，等于用图谱把候选池重新填满一遍。
NEIGHBOURS_PER_SEED: Final[int] = 2
#: 注入项上限（候选池上限之下那一层）：重排有成本，池子不能无限扩。
MAX_INJECTED: Final[int] = 30

#: 边型的优先级：引用是「文档作者亲自指的」，兄弟节是「上下文连续」，父节是「够读」。
#: 三个都在 ``app.models.section_edge`` 里定义过一次，这里只排顺序、不重写字符串。
RELATION_PRIORITY: Final[tuple[str, ...]] = (
    RELATION_REFERENCE,
    RELATION_SIBLING,
    RELATION_PARENT,
)

#: 注入项的出处标记（ADR-0027 决策 4）。**只有注入项带它**：直接命中项不带任何来源键，
#: 于是「直接命中」与「按关系补进来」在响应里可区分（#147 的 6 个保留位也按它数）。
INJECTED_SOURCE: Final[str] = "graph_injection"

#: 降级原因：一因一码。与 ``vector search failed`` / ``rerank failed`` 同一层级。
REASON_GRAPH_FAILED: Final[str] = "graph injection failed"


@dataclass(frozen=True)
class SeedSection:
    """一个种子节的身份（三元组，不带版本）：从融合结果头部那几条命中的 meta 里读出来。"""

    kb_id: str
    document_id: str
    section_id: str


@dataclass(frozen=True)
class InjectedSection:
    """一条**候选**：目标节的一个代表片 + 它是被哪种边、从哪个种子拉进来的。

    ``relation_type`` 与 ``seed_section_id`` 只为观测与排障存在（``by_relation`` 那一栏），
    不参与排序：候选进池之后的待遇与直接命中完全一样，只是 ``score`` 是 0。
    """

    kb_id: str
    index_version: str
    document_id: str
    section_id: str
    chunk_id: str
    content: str
    title: str
    heading_path: str
    relation_type: str
    seed_section_id: str = ""


@dataclass(frozen=True)
class InjectionOutcome:
    """注入之后交回的三样事实：候选池、注入了什么、这一步有没有降级。

    ``by_relation`` 是「注入条数的来源分布」（``{"reference": 1, "sibling": 2}``）：
    观测要能回答「这次是被引用拉进来的，还是被兄弟节拉进来的」，而不是只有一个总数。
    """

    chunks: list[dict]
    injected: int = 0
    seeds: int = 0
    by_relation: Mapping[str, int] = field(default_factory=dict)
    degraded_reason: str | None = None

    def as_metadata(self) -> dict[str, object]:
        """给响应 ``metadata.retrieval.graph`` 用：只放可观测的计数，不放内容。"""

        return {
            "injected": self.injected,
            "seeds": self.seeds,
            "by_relation": dict(self.by_relation),
        }


class SectionGraph(Protocol):
    """图谱的**读模型**（注入缝要替身的就是它，与 ``rerank_factory`` 同形）。

    两个方法而不是一个：种子身份要读命中的 ``meta``，邻居要读边与代表片，两者的数据
    来源不同、失败语义相同（都属于「关系查询」这一条降级原因）。
    """

    async def seed_sections(
        self,
        *,
        tenant_id: str,
        index_versions: Mapping[str, str],
        chunks: Sequence[Mapping[str, Any]],
    ) -> list[SeedSection]: ...

    async def neighbours(
        self,
        *,
        tenant_id: str,
        index_versions: Mapping[str, str],
        seeds: Sequence[SeedSection],
    ) -> list[InjectedSection]: ...


#: 注入位的形状（内核的扩展点）：吃「当前的候选池 + 本次范围」，回「新的候选池 + 事实」。
GraphInjection = Callable[[list[dict], Mapping[str, str]], Awaitable[InjectionOutcome]]

#: 图谱实现的工厂：**吃请求会话**，回读模型。与 ``rerank_factory`` 一样是注入缝，但形状
#: 多一个会话参数——图谱要读库（边与代表片），而重排 Provider 不读库。把会话藏在闭包里
#: 也行，那样「这个工厂会用到哪个会话」就只剩约定；显式递进来才与组合根同一份范围。
GraphFactory = Callable[[AsyncSession], SectionGraph]


# ─────────────────────────────────────────────────── 数据库那一侧（生产的读模型）


class DbSectionGraph:
    """生产实现：边与代表片都从库里读，范围用调用方解析好的 ``kb_id → 版本`` 映射。

    它**不判断要不要注入**（那是服务层与组合根的事），也不决定注入几条（同上）：
    这里只回答「这批种子的邻居是谁、每个邻居的代表片长什么样」。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def seed_sections(
        self,
        *,
        tenant_id: str,
        index_versions: Mapping[str, str],
        chunks: Sequence[Mapping[str, Any]],
    ) -> list[SeedSection]:
        kb_ids = sorted({str(chunk["kb_id"]) for chunk in chunks if chunk.get("kb_id")})
        chunk_ids = [str(chunk["chunk_id"]) for chunk in chunks if chunk.get("chunk_id")]
        if not kb_ids or not chunk_ids:
            return []
        metas = await chunk_repository.fetch_meta_by_chunk_ids(
            self._db, tenant_id=tenant_id, kb_ids=kb_ids, chunk_ids=chunk_ids
        )
        seeds: list[SeedSection] = []
        seen: set[tuple[str, str, str]] = set()
        for chunk in chunks:
            kb_id = chunk.get("kb_id")
            document_id = chunk.get("document_id")
            if not isinstance(kb_id, str) or not isinstance(document_id, str):
                continue
            # 范围外的库不可能是本次的种子：它的边即便存在也不该参与这次检索。
            if kb_id not in index_versions:
                continue
            meta = metas.get((kb_id, str(chunk.get("chunk_id") or ""))) or {}
            section_id = str(meta.get("section_id") or "")
            if not section_id:
                # 无标题文档没有节身份，也就没有节级关系可扩（ADR-0027 决策 1）。
                continue
            key = (kb_id, document_id, section_id)
            if key in seen:
                continue
            seen.add(key)
            seeds.append(SeedSection(kb_id=kb_id, document_id=document_id, section_id=section_id))
        return seeds

    async def neighbours(
        self,
        *,
        tenant_id: str,
        index_versions: Mapping[str, str],
        seeds: Sequence[SeedSection],
    ) -> list[InjectedSection]:
        edges = await section_edge_repository.fetch_outbound(
            self._db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            seeds=[(seed.kb_id, seed.document_id, seed.section_id) for seed in seeds],
        )
        if not edges:
            return []
        representatives = await chunk_repository.fetch_section_representatives(
            self._db,
            tenant_id=tenant_id,
            index_versions=index_versions,
            targets=[
                (edge.kb_id, edge.target_document_id, edge.target_section_id) for edge in edges
            ],
        )
        neighbours: list[InjectedSection] = []
        for edge in edges:
            key = (edge.kb_id, edge.target_document_id, edge.target_section_id)
            representative = representatives.get(key)
            if representative is None:
                # 目标节在本版本里没有可注入的片（纯标题节 / 只有路标片）：跳过它，
                # 而不是注入一条空正文。
                continue
            chunk_id, content, title, heading_path = representative
            neighbours.append(
                InjectedSection(
                    kb_id=edge.kb_id,
                    index_version=edge.index_version,
                    document_id=edge.target_document_id,
                    section_id=edge.target_section_id,
                    chunk_id=chunk_id,
                    content=content,
                    title=title,
                    heading_path=heading_path,
                    relation_type=edge.relation_type,
                    seed_section_id=edge.source_section_id,
                )
            )
        return neighbours


# ─────────────────────────────────────────────────────────── 策略（纯函数）


def _priority(relation_type: str) -> int:
    """优先级排序键：认不出第四种边型时排最后（而不是排在引用前面）。"""

    try:
        return RELATION_PRIORITY.index(relation_type)
    except ValueError:  # pragma: no cover - 边型的词表由 CHECK 约束钉死
        return len(RELATION_PRIORITY)


def select_candidates(
    *,
    seeds: Sequence[SeedSection],
    neighbours: Sequence[InjectedSection],
    taken_sections: Sequence[tuple[str, str]],
    taken_chunk_ids: Sequence[str],
    limit: int = MAX_INJECTED,
) -> list[InjectedSection]:
    """按「种子顺序 → 边型优先级」挑出要注入的候选（纯函数，不碰库）。

    三条上限在同一次遍历里一起生效：每个种子 ≤ :data:`NEIGHBOURS_PER_SEED`、总数
    ≤ ``limit``；此外**一节只进一次**（``taken_sections`` 是种子的节与本次已注入的节的
    并集，同一个目标片也只进一次）。

    ``taken_sections`` / ``taken_chunk_ids`` 由调用方给：池子里已经有的东西不该被
    「注入」第二次——那既不会增加信息，还会白占一个重排名额。**能核到的只有身份**：
    召回行不带 ``section_id``（那在 ``meta`` 里），所以「同节的其他片已经在池子里」这
    种情况核不出来；折叠开着时它会被折成同一个交付单元（不会多占名额），折叠关掉时
    表现为同一节多一条结果——这是「不为一次判重再查一遍库」换来的，代价写在这里。
    """

    taken = set(taken_sections)
    taken_chunks = set(taken_chunk_ids)
    selected: list[InjectedSection] = []
    for seed in seeds:
        if len(selected) >= limit:
            break
        own = [
            neighbour for neighbour in neighbours if neighbour.seed_section_id == seed.section_id
        ]
        own.sort(key=lambda neighbour: _priority(neighbour.relation_type))
        count = 0
        for neighbour in own:
            if count >= NEIGHBOURS_PER_SEED or len(selected) >= limit:
                break
            section_key = (neighbour.kb_id, neighbour.section_id)
            if section_key in taken or neighbour.chunk_id in taken_chunks:
                continue
            if not neighbour.content:
                # 空正文的候选进池只会占名额：它不是材料。
                continue
            taken.add(section_key)
            taken_chunks.add(neighbour.chunk_id)
            selected.append(neighbour)
            count += 1
    return selected


def _injected_row(neighbour: InjectedSection) -> dict[str, Any]:
    """把一条候选变成**与召回行同形**的字典。

    五个分列字段一律 ``None``（不是 0）：注入项没有在任何一路召回里得分，写 0 会被读成
    「命中了但很不相关」。``score`` 是唯一有意义的值，而且它恒为 0——名次交给重排。
    """

    return with_score_columns(
        {
            "document_id": neighbour.document_id,
            "chunk_id": neighbour.chunk_id,
            "title": neighbour.title,
            "content": neighbour.content,
            "kb_id": neighbour.kb_id,
            "score": 0.0,
            "section_id": neighbour.section_id,
            "heading_path": neighbour.heading_path,
            "retrieval_source": INJECTED_SOURCE,
        }
    )


# ─────────────────────────────────────────────────────────── 服务入口


async def inject_candidates(
    *,
    graph: SectionGraph,
    tenant_id: str,
    index_versions: Mapping[str, str],
    chunks: Sequence[Mapping[str, Any]],
) -> InjectionOutcome:
    """在融合结果之后扩一批候选；关系查询失败只降级、不抛。

    ``chunks`` 是**当前候选池**（按融合顺序）。种子取它的前 :data:`MAX_SEEDS` 条，注入项
    追加在**末尾**——它们的 ``score`` 是 0，排在前面会让人误以为图谱裁决了名次。

    失败路径返回的候选池与传入的**逐字节相同**：主链路照跑，只多一条降级原因。
    """

    current: list[dict[str, Any]] = [dict(chunk) for chunk in chunks]
    if not current:
        return InjectionOutcome(chunks=current)

    seed_rows = current[:MAX_SEEDS]
    try:
        seeds = await graph.seed_sections(
            tenant_id=tenant_id, index_versions=index_versions, chunks=seed_rows
        )
        if not seeds:
            # 没有节身份的命中（无标题文档）不扩：节级关系在这里无从下手，那不是故障。
            return InjectionOutcome(chunks=current)
        neighbours = await graph.neighbours(
            tenant_id=tenant_id, index_versions=index_versions, seeds=seeds
        )
    except Exception as exc:  # noqa: BLE001 - 旁路能力失败绝不冒泡成整单失败
        # 只记类型，不记原文：异常文案可能夹带切片内容（与通道故障、重排同一口径）。
        logger.warning(
            "graph injection failed",
            tenant_id=tenant_id,
            kb_ids=list(index_versions),
            error=type(exc).__name__,
        )
        return InjectionOutcome(chunks=current, degraded_reason=REASON_GRAPH_FAILED)

    # 第二道门：库里读回来的东西再按本次范围核一遍（见模块文档第 3 条）。
    in_scope = [
        neighbour
        for neighbour in neighbours
        if index_versions.get(neighbour.kb_id) == neighbour.index_version
    ]
    selected = select_candidates(
        seeds=seeds,
        neighbours=in_scope,
        taken_sections=[(seed.kb_id, seed.section_id) for seed in seeds],
        taken_chunk_ids=[str(chunk["chunk_id"]) for chunk in current if chunk.get("chunk_id")],
    )
    if not selected:
        return InjectionOutcome(chunks=current, seeds=len(seeds))

    by_relation: dict[str, int] = {}
    for neighbour in selected:
        by_relation[neighbour.relation_type] = by_relation.get(neighbour.relation_type, 0) + 1
    logger.info(
        "graph injection applied",
        tenant_id=tenant_id,
        kb_ids=list(index_versions),
        seeds=len(seeds),
        injected=len(selected),
        by_relation=by_relation,
    )
    return InjectionOutcome(
        chunks=[*current, *(_injected_row(neighbour) for neighbour in selected)],
        injected=len(selected),
        seeds=len(seeds),
        by_relation=by_relation,
    )


def build_graph_injection(
    *,
    db: AsyncSession,
    tenant_id: str,
    graph_factory: GraphFactory,
) -> GraphInjection:
    """把「库 + 租户 + 工厂」绑成一个内核能直接调用的注入位（组合根装配用）。

    ``graph_factory`` 的生产实现就是 :class:`DbSectionGraph`（组合根的缺省值）。评测 /
    用例传一个别的工厂进来就替身掉整个图谱读模型——一个开关覆盖三种检索方式，因为注入位
    在「候选池成形之后」这个位置，三种方式都会经过它。
    """

    graph = graph_factory(db)

    async def _inject(chunks: list[dict], index_versions: Mapping[str, str]) -> InjectionOutcome:
        return await inject_candidates(
            graph=graph,
            tenant_id=tenant_id,
            index_versions=index_versions,
            chunks=chunks,
        )

    return _inject


__all__ = [
    "INJECTED_SOURCE",
    "MAX_INJECTED",
    "MAX_SEEDS",
    "NEIGHBOURS_PER_SEED",
    "REASON_GRAPH_FAILED",
    "RELATION_PRIORITY",
    "DbSectionGraph",
    "GraphFactory",
    "GraphInjection",
    "InjectedSection",
    "InjectionOutcome",
    "SectionGraph",
    "SeedSection",
    "build_graph_injection",
    "inject_candidates",
    "select_candidates",
]
