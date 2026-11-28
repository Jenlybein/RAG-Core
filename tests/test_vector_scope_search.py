"""范围版向量召回：配对条件编译成什么样（离线单测，不上真库）。

**为什么单独一个文件**：这里要钉的不是「距离算得对不对」，而是「查询条件会不会
拼出一个不存在的组合」。两种写法的诱惑就在于此：

* 错误写法一：按 (kb, 版本) 配对——库级版本已删除，范围只按库
* 错误写法二：两个独立 IN 再拼版本

单库单版本时两者**完全等价**，怎么测都绿；只有多库多版本才分叉——后者会匹配出
「A 库 + B 的版本」这种组合，在多版本并存时就是越权或脏数据。所以只有把编译器
的产物变成字符串来断言，才守得住这件事。

接缝：``build_scope_filter`` 是纯函数（返回 SQLAlchemy 表达式，不碰数据库、不发
SQL）。用 SQLite 方言编译即可离线断言；真要跑 SQL 得先有 pgvector 的
``cosine_distance``，那是另一层的事（见 ``tests/test_vector_search.py``）。

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


# ─────────────────────────────────────────── 范围：库集合（不再带版本）


def test_scope_filter_is_the_kb_set() -> None:
    """范围条件编译成 ``chunks.kb_id IN (...)``（现场九·第四步）。

    改造前它是「库 + 生效版本」的配对条件（每对内部 AND、对之间 OR）。代下放到篇级之后
    配对不再存在：一次检索里每篇各有自己的当前代，可见性由 ``current_generation_filter()``
    表达，范围只剩「哪几个库」。
    """

    sql = _normalized(_compiled(build_scope_filter([KB_A, KB_B])))

    assert sql == "chunks.kb_id IN ('kb-a', 'kb-b')"


def test_scope_filter_never_carries_index_version() -> None:
    """版本一旦回到范围条件里，构建期间刚建好的那篇就会从召回里消失。

    真库实测过：那篇的切片在新版本里、库的生效版本还是旧的，两个条件相乘 → 0 片。
    """

    sql = _normalized(_compiled(build_scope_filter([KB_A, KB_B])))

    assert "index_version" not in sql


def test_scope_filter_for_a_single_kb() -> None:
    sql = _normalized(_compiled(build_scope_filter([KB_A])))

    assert sql == "chunks.kb_id IN ('kb-a')"


def test_scope_filter_with_empty_scope_matches_nothing() -> None:
    """空范围必须编译成「什么都不匹配」，绝不能退化成「不过滤」。

    ``IN ()`` 在 SQLAlchemy 里渲染成一个恒假条件；关键断言是它渲染出了**恒假**，
    而不是空片段——空片段意味着 ``WHERE tenant_id = ?`` 之后什么都不剩。
    """

    sql = _normalized(_compiled(build_scope_filter([]))).upper()

    assert "WHERE 1!=1" in sql or "IN (NULL)" in sql, "不过滤等于跨库召回，是比空结果严重得多的事故"


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
            kb_ids=[KB_A, KB_B],
            top_k=7,
        )
    )

    assert rows == [], "替身不返回行，这里只关心发出去的语句"
    sql = _normalized(str(session.statement.compile(dialect=postgresql.dialect())))

    assert sql.upper().count("LIMIT") == 1, "一个 LIMIT = 一次全局截断"
    assert " ORDER BY DISTANCE LIMIT " in sql.upper(), "先全局按距离排序，再截断"
    upper = sql.upper()
    # 范围是「库集合」，可见性是「当前代 × 当前稿」（现场九·第四步）：三条都在 WHERE 里，
    # 版本那条一个都不许回来。
    assert "CHUNKS.KB_ID IN (" in upper and "KB_ID_1" in upper, "范围是库集合"
    assert "CHUNKS.GENERATION = DOCUMENTS.CURRENT_INDEX_GENERATION" in upper, "按代的可见性"
    assert "CHUNKS.DOC_VERSION = DOCUMENTS.CURRENT_VERSION" in upper, "按稿的可见性"
    assert "INDEX_VERSION" not in upper, "库级版本不再是可见性判据"
    assert "CHUNKS.TENANT_ID = " in upper, "租户过滤是强制条件（ADR-0006）"
