"""范围版向量召回：配对条件编译成什么样（离线单测，不上真库）。

**为什么单独一个文件**：这里要钉的不是「距离算得对不对」，而是「查询条件会不会
拼出一个不存在的组合」。两种写法的诱惑就在于此：

* 配对条件：``(kb_id = 'kb-a' AND index_version = 'v1') OR (kb_id = 'kb-b' AND ...)``
* 两个独立 IN：``kb_id IN ('kb-a','kb-b') AND index_version IN ('v1','v2')``

单库单版本时两者**完全等价**，怎么测都绿；只有多库多版本才分叉——后者会匹配出
「A 库 + B 的版本」这种组合，在多版本并存时就是越权或脏数据。所以只有把编译器
的产物变成字符串来断言，才守得住这件事。

接缝：``build_scope_filter`` 是纯函数（返回 SQLAlchemy 表达式，不碰数据库、不发
SQL）。用 SQLite 方言编译即可离线断言；真要跑 SQL 得先有 pgvector 的
``cosine_distance``，那是另一层的事（见 ``tests/test_index_version.py``）。

最后一个用例直接调真实实现（``PgVectorStore.similarity_search_scope``），用一个
只记账的会话替身接住语句：不上真库，但断言的是**真的会发出去的那条 SQL**——它只
有一条、只有一个 ``LIMIT``。「每库各取 top_k 再合并」在这里就会露出两个 ``LIMIT``
或两次调用。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql, sqlite

from app.providers.vectorstores.pgvector import PgVectorStore, build_scope_filter

KB_A = "kb-a"
KB_B = "kb-b"


def _compiled(clause) -> str:
    """把表达式编译成带字面量的 SQL 片段（与库无关，只为看结构）。"""

    return str(clause.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))


def _normalized(sql: str) -> str:
    """压平空白，让断言只对结构敏感、不对排版敏感。"""

    return " ".join(sql.split())


# ─────────────────────────────────────────── 配对：每对内部 AND，对之间 OR


def test_scope_filter_pairs_each_kb_with_its_own_version() -> None:
    """两对「库 + 版本」时，编译结果必须是「每对内部 AND、对之间 OR」的形状。

    SQLAlchemy 不在 ``OR`` 的每个分支外面加括号——SQL 里 ``AND`` 的优先级本来就
    高于 ``OR``，``A AND B OR C AND D`` 与 ``(A AND B) OR (C AND D)`` 等价。所以
    这里断言渲染出的字符串，另有一个结构化断言（下一个用例）兜住优先级这件事。
    """

    sql = _normalized(_compiled(build_scope_filter({KB_A: "v1", KB_B: "v2"})))

    # 期望值按父 spec「关键词通道 / 数据模型」手写：配对条件就该是这两段 AND 由一个
    # OR 连起来，而不是从实现里回读。
    assert sql == (
        "chunks.kb_id = 'kb-a' AND chunks.index_version = 'v1'"
        " OR "
        "chunks.kb_id = 'kb-b' AND chunks.index_version = 'v2'"
    )


def test_scope_filter_is_an_or_of_ands_not_two_in_lists() -> None:
    """结构化断言：条件树的根是 ``OR``，下面每个分支都是 ``AND``（库 + 版本同时成立）。"""

    from sqlalchemy.sql import operators

    clause = build_scope_filter({KB_A: "v1", KB_B: "v2"})

    assert clause.operator is operators.or_, "对与对之间必须是 OR"
    assert [branch.operator for branch in clause.clauses] == [operators.and_, operators.and_]
    # 两对 = 两个分支；三个分支就说明有人把「每对」拆成了独立条件。
    assert len(clause.clauses) == 2
    # 每个分支里恰好两个叶子：库与版本，缺一个就是漏过滤。
    assert [len(branch.clauses) for branch in clause.clauses] == [2, 2]


def test_scope_filter_never_degrades_into_two_independent_in_lists() -> None:
    """反例守卫：两个独立 ``IN`` 正是本票要禁止的写法。"""

    sql = _normalized(_compiled(build_scope_filter({KB_A: "v1", KB_B: "v2"}))).upper()

    assert " IN " not in sql, "独立 IN 会匹配出「A 库的 v2」这种不存在的组合"
    assert sql.count(" OR ") == 1, "两对之间恰好一个 OR；再多就是在堆独立条件"
    assert " AND " in sql, "每对内部必须是 AND（库与版本同时成立）"


def test_scope_filter_for_a_single_pair_is_a_plain_and() -> None:
    """一个库时退化成普通 AND：与单库版 ``similarity_search`` 的口径一致。"""

    sql = _normalized(_compiled(build_scope_filter({KB_A: "v1"})))

    assert sql == "chunks.kb_id = 'kb-a' AND chunks.index_version = 'v1'"
    assert " OR " not in sql


def test_scope_filter_with_empty_scope_matches_nothing() -> None:
    """空范围必须编译成「什么都不匹配」，绝不能退化成「不过滤」。

    ``false()`` 在 SQLite 方言下渲染成常量 ``0``；关键断言是它渲染出了一个**恒假
    条件**，而不是空片段——空片段意味着 ``WHERE tenant_id = ?`` 之后什么都不剩。
    """

    sql = _normalized(_compiled(build_scope_filter({})))

    assert sql == "0", "不过滤等于跨库召回，是比空结果严重得多的事故"


# ───────────────────────────── 真的会发出去的那条 SQL：只有一条、一个 LIMIT


class _CapturingSession:
    """**异步**会话替身：只把语句接住，不连数据库、不执行（#127 起读侧是 ``await``）。"""

    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(all=lambda: [])


def test_scope_search_sends_one_statement_with_one_global_limit() -> None:
    """范围召回是一条 SQL + 一个 ``LIMIT``：全局截断，不是每库各截一次。

    「每库各取 top_k 再合并」在这里会露馅：要么两次调用，要么 ``LIMIT`` 不止一个。
    顺便钉住租户过滤与配对条件都真的进了 ``WHERE``——只有构造函数的单测还不够，
    这里跑的是实现本身。
    """

    session = _CapturingSession()

    rows = asyncio.run(
        PgVectorStore().similarity_search_scope(
            session,
            [0.1, 0.2, 0.3],
            tenant_id="tenant-1",
            index_versions={KB_A: "v1", KB_B: "v2"},
            top_k=7,
        )
    )

    assert rows == [], "替身不返回行，这里只关心发出去的语句"
    sql = _normalized(str(session.statement.compile(dialect=postgresql.dialect())))

    assert sql.upper().count("LIMIT") == 1, "一个 LIMIT = 一次全局截断"
    assert " ORDER BY DISTANCE LIMIT " in sql.upper(), "先全局按距离排序，再截断"
    assert " IN " not in sql.upper(), "配对条件不是独立的 IN"
    assert sql.count("chunks.kb_id = ") == 2 and sql.count("chunks.index_version = ") == 2
    assert "chunks.tenant_id = " in sql, "租户过滤是强制条件（ADR-0006）"
    assert " OR " in sql.upper()
