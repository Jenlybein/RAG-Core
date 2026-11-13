"""关系边的写入与清理（语句构造是纯函数，执行很薄）。

四条语句，每条都回答一个具体问题：

| 函数 | 回答什么 |
| --- | --- |
| :func:`build_delete_for_document_statement` | 重建这一篇之前，它在本版本里的边（出边与入边）怎么清 |
| :func:`build_insert_statement` | 新抽出来的边怎么幂等写入（``ON CONFLICT DO NOTHING``） |
| :func:`build_prune_statement` | 本版本里哪些边已经指向不存在的节（悬空边） |
| :func:`count_by_scope` | 这一版本里有多少条边（核对 / 排障读数） |

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

from collections.abc import Iterable, Sequence
from typing import Final, cast

from sqlalchemy import ColumnElement, Table, and_, delete, func, not_, or_, select, tuple_
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
    "build_prune_statement",
    "build_scope_clause",
    "count_by_scope",
    "delete_for_document",
    "insert_many",
    "list_for_document",
    "prune_dangling",
]
