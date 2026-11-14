"""关系边的写入与清理（语句构造是纯函数，执行很薄）。

四条语句，每条都回答一个具体问题：

| 函数 | 回答什么 |
| --- | --- |
| :func:`build_delete_for_document_statement` | 重建这一篇之前，它在本版本里的边（出边与入边）怎么清 |
| :func:`build_insert_statement` | 新抽出来的边怎么幂等写入（``ON CONFLICT DO NOTHING``） |
| :func:`build_prune_statement` | 本版本里哪些边已经指向不存在的节（悬空边） |
| :func:`count_by_scope` | 这一版本里有多少条边（核对 / 排障读数） |
| :func:`build_outbound_statement` | 检索侧的**一跳出边**：这批种子节在本版本里指向了谁（#144 的候选注入） |

**检索侧那一条为什么也住在这里**（票 #144）：注入要问「种子节的邻居是谁」，而这条
查询的**范围条件**必须与写入侧同一份形状——三列作用域、映射解析出来的配对。放到
``app/services/`` 里另写一遍，等于让「哪条边算本版本的边」有了两个答案；而这种分叉
的表现是「注入了另一个库的一条」，不报错。

**为什么先删后写、还要再清悬空边**（ADR-0025 + ADR-0027）：

1. 先删该文档的边：它的节/正文变了，旧边是过期的；
2. 再写新边；
3. 再清一遍悬空：**其他文档指向它的边**也可能指向已经不存在的节——只删一侧的话，
   重建之后库里就会留下「指向一个不存在的节」的边，检索时拉不到东西却不报错。

清悬空用的是「本版本里全部合法节对」的 ``NOT IN``，一条语句做完：按行读回再逐条删
在文档多、边多时是 N² 次往返，而这件事每重建一篇文档就要做一次。

**语句为什么是纯函数**：``ON CONFLICT``、行值 ``IN``、作用域三列这些形状只有编译出来
才看得见，而它们正是「多删一份、少删一个租户」这类事故的所在。用例直接断言编译结果，
不需要连 PostgreSQL（同一套写法见 ``tests/test_vector_scope_search.py``）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, cast

from sqlalchemy import ColumnElement, Table, and_, delete, false, func, not_, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Delete, Insert, Select

from app.db.base import now
from app.models.section_edge import UNIQUE_EDGE_COLUMNS, SectionEdge
from app.services.section_graph import EdgeDraft, EdgeScope
from app.utils.id_generator import new_id

#: 写入时带的列（``id`` / ``created_at`` 由这一层补，证据列缺省空对象）。
_INSERT_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "kb_id",
    "index_version",
    "source_document_id",
    "source_section_id",
    "target_document_id",
    "target_section_id",
    "relation_type",
)


def build_scope_clause(scope: EdgeScope) -> ColumnElement[bool]:
    """作用域条件：三列都要，少一列就会跨版本或跨租户读/删到别人的边。"""

    return and_(
        SectionEdge.tenant_id == scope.tenant_id,
        SectionEdge.kb_id == scope.kb_id,
        SectionEdge.index_version == scope.index_version,
    )


def build_scopes_clause(
    *, tenant_id: str, index_versions: Mapping[str, str]
) -> ColumnElement[bool]:
    """**本次请求的范围**：``kb_id → 索引版本`` 的映射编译成「对与对之间 OR」。

    与 ``app.models.chunk.scope_filter`` 是同一条纪律的第二次落地：配对关系只能从映射
    里读出来。写成 ``kb_id IN (...)`` 配 ``index_version IN (...)`` 会匹配出「A 库配
    B 库版本」这种不存在的组合——那是越权，而且不报错。

    空映射回**恒假**：拿不到范围却不过滤，等于把整个租户的边都读进来。
    """

    pairs = [
        and_(
            SectionEdge.tenant_id == tenant_id,
            SectionEdge.kb_id == kb_id,
            SectionEdge.index_version == index_version,
        )
        for kb_id, index_version in index_versions.items()
    ]
    if not pairs:
        return false()
    return or_(*pairs)


def build_outbound_statement(
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    seeds: Sequence[tuple[str, str, str]],
) -> Select[tuple[SectionEdge]]:
    """一跳出边：这批 ``(kb_id, 文档 id, 节 id)`` 在本版本里指向了谁。

    **只查一跳**（父 spec「Out of Scope」：不做多跳图遍历）。种子为空时不发文——
    空 ``IN`` 在 PG 里是语法合法但恒假的写法，这里提前返回省掉一次往返。

    返回的是**边**而不是节点：目标节该不该被注入由服务层按「引用 > 兄弟节 > 父节」的
    优先级裁决（:mod:`app.services.graph_injection`），查询只负责「有哪些边」。
    """

    statement = select(SectionEdge).where(
        build_scopes_clause(tenant_id=tenant_id, index_versions=index_versions)
    )
    pairs = sorted({(str(document_id), str(section_id)) for _, document_id, section_id in seeds})
    if not pairs:
        return statement.where(false())
    return statement.where(
        tuple_(SectionEdge.source_document_id, SectionEdge.source_section_id).in_(pairs)
    )


async def fetch_outbound(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    seeds: Sequence[tuple[str, str, str]],
) -> list[SectionEdge]:
    """跳数=1 的邻居边（**不做跨库禁止**：跨库边允许存在，范围由上面的条件过滤）。"""

    rows = (
        await db.execute(
            build_outbound_statement(
                tenant_id=tenant_id, index_versions=index_versions, seeds=seeds
            )
        )
    ).scalars()
    return list(rows.all())


def build_delete_for_document_statement(scope: EdgeScope, document_id: str) -> Delete:
    """删该文档在本版本里的**全部**边：它作为起点与它作为终点都算。

    只删出边的话，别的文档指向这篇旧节的边会留下来，而重建后那些节可能已经不存在
    ——那正是「悬空边」最典型的来源。
    """

    return delete(SectionEdge).where(
        build_scope_clause(scope),
        or_(
            SectionEdge.source_document_id == document_id,
            SectionEdge.target_document_id == document_id,
        ),
    )


def build_insert_statement(drafts: Sequence[EdgeDraft]) -> Insert:
    """幂等写入：唯一约束撞上时**不报错、不覆盖**（同一条边再抽一次就是同一条边）。

    **走表而不是 ORM 实体**（``SectionEdge.__table__``）：值字典里的键是列名，而 ORM 实体上
    的 ``metadata`` 是 ``Table.metadata``（一个 ``MetaData``），不是证据列——ORM 解释这条
    ``INSERT`` 时会把 ``metadata`` 当成实体属性，炸出 ``MetaData has no attribute
    _bulk_update_tuples``。边只是「一些行」，不需要 ORM 的单位工作与同步策略。
    """

    rows = [
        {
            "id": new_id(),
            "created_at": now(),
            **{column: getattr(draft, column) for column in _INSERT_COLUMNS},
            "metadata": dict(draft.metadata),
        }
        for draft in drafts
    ]
    return (
        # ``__table__`` 在类型上标成 FromClause，运行期就是那张表；``cast`` 只补类型。
        pg_insert(cast(Table, SectionEdge.__table__))
        .values(rows)
        .on_conflict_do_nothing(index_elements=list(UNIQUE_EDGE_COLUMNS))
    )


def build_prune_statement(scope: EdgeScope, valid_pairs: Iterable[tuple[str, str]]) -> Delete:
    """删掉本版本里两端**至少一端**已不存在的边（悬空边）。

    ``valid_pairs`` 是本版本里真实存在的 ``(文档 id, 节 id)`` 全集。空集合时整版清空：
    一版本里一个节都没有，就没有一条边是合法的。
    """

    pairs = sorted({(str(document_id), str(section_id)) for document_id, section_id in valid_pairs})
    statement = delete(SectionEdge).where(build_scope_clause(scope))
    if not pairs:
        return statement
    rows = [(document_id, section_id) for document_id, section_id in pairs]
    source_alive = tuple_(SectionEdge.source_document_id, SectionEdge.source_section_id).in_(rows)
    target_alive = tuple_(SectionEdge.target_document_id, SectionEdge.target_section_id).in_(rows)
    return statement.where(or_(not_(source_alive), not_(target_alive)))


async def delete_for_document(db: AsyncSession, *, scope: EdgeScope, document_id: str) -> None:
    """:func:`build_delete_for_document_statement` 的执行面（不提交，事务归调用方）。"""

    await db.execute(build_delete_for_document_statement(scope, document_id))


async def insert_many(db: AsyncSession, drafts: Sequence[EdgeDraft]) -> None:
    """:func:`build_insert_statement` 的执行面。空集合不发文（空 ``VALUES`` 是语法错）。"""

    if not drafts:
        return
    await db.execute(build_insert_statement(drafts))


async def prune_dangling(
    db: AsyncSession, *, scope: EdgeScope, valid_pairs: Iterable[tuple[str, str]]
) -> None:
    """:func:`build_prune_statement` 的执行面。"""

    await db.execute(build_prune_statement(scope, valid_pairs))


def build_count_statement(scope: EdgeScope) -> Select[tuple[int]]:
    return select(func.count()).select_from(SectionEdge).where(build_scope_clause(scope))


async def count_by_scope(db: AsyncSession, *, scope: EdgeScope) -> int:
    """这一版本里的边数（核对脚本与排障用；不参与检索）。"""

    return int((await db.execute(build_count_statement(scope))).scalar_one())


def build_list_for_document_statement(scope: EdgeScope, document_id: str) -> Select:
    """列该文档在本版本里的边（排障 / 用例断言用）。"""

    return select(SectionEdge).where(
        build_scope_clause(scope),
        or_(
            SectionEdge.source_document_id == document_id,
            SectionEdge.target_document_id == document_id,
        ),
    )


async def list_for_document(
    db: AsyncSession, *, scope: EdgeScope, document_id: str
) -> list[SectionEdge]:
    rows = (await db.execute(build_list_for_document_statement(scope, document_id))).scalars()
    return list(rows.all())


__all__ = [
    "build_count_statement",
    "build_delete_for_document_statement",
    "build_insert_statement",
    "build_list_for_document_statement",
    "build_outbound_statement",
    "build_prune_statement",
    "build_scope_clause",
    "build_scopes_clause",
    "count_by_scope",
    "delete_for_document",
    "fetch_outbound",
    "insert_many",
    "list_for_document",
    "prune_dangling",
]
