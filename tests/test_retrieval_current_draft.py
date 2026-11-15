"""#174 · 默认检索只看**当前稿**：五个口径一次对齐。

父 spec 把话说得很直：这条约束必须落在 SQL 与关键词索引的 ``filter`` 里，**不靠应用层
「记得过滤」**。于是本票要守的是同一个谓词在五处逐字相同：

1. 向量召回（``similarity_search`` / ``similarity_search_scope``）；
2. 关键词召回（``build_bm25_query`` 的 ``is_current`` term）；
3. 折叠取整节（``_unit_rows_statement`` / 父节正文）；
4. 空结果原因的计数（``_count_by_kb_ids_statement``）；
5. 溯源回查（``_meta_statement``：按切片 id 反查元数据 + 稿号）。

为什么值得为「一条 SQL 条件」写一整个文件：两稿切片并存的时刻（指针已指向 v2、v1 的
切片按保留策略还在）**只有靠过滤才不出现**，而漏掉任意一处都不报错——向量路会多召回
一份旧稿，折叠会把两稿拼成一条结果，计数会说「库里有内容」而召回一条都没有，溯源回查
会把旧稿的切片当成当前稿的。症状全是「结果看起来正常，只是多了一份/少了一份」。

接缝：与既有用例一致——纯函数编译成 SQL 断言（``test_vector_scope_search`` 的路子），
执行层用注入的假客户端 / 只记账的会话（``test_keyword_versioned_index`` 的路子）。
不上真库、不连 ES。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from app.providers.vectorstores.pgvector import PgVectorStore

TENANT = "tenant_demo"
KB = "kb-a"


def _sql(clause_or_statement: Any) -> str:
    """把表达式 / 语句编译成压平空白的 SQL，断言只对结构敏感、不对排版敏感。

    **不渲染字面量**：向量列是 ``Vector(1536)``，SQLAlchemy 没有它的字面量处理器
    （``test_vector_scope_search`` 里那条「真的会发出去的 SQL」也因此不用
    ``literal_binds``）。这里断言的是列与列的**比较**，本来就不需要值。
    """

    compiled = clause_or_statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split())


class _CapturingSession:
    """只接住语句、不执行的异步会话替身（不连库、不发 SQL）。"""

    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> SimpleNamespace:
        self.statement = statement
        return SimpleNamespace(all=lambda: [])


# ───────────────────────────────────────────────────── 一、向量召回
# 「同一文档两稿各一份切片、指针指向 v2」这条验收就钉在这里和后面的
# ``test_only_the_two_recall_paths_deliver_the_current_draft``：向量路的谓词是
# 「切片稿号 = 文档当前稿指针」，一个 JOIN 就把「谁是当前稿」交给库算，应用层不缓存指针。


def test_vector_recall_reads_only_the_current_draft() -> None:
    """范围版召回：``chunks.doc_version = documents.current_version`` 必须在 WHERE 里。"""

    session = _CapturingSession()

    rows = asyncio.run(
        PgVectorStore().similarity_search_scope(
            session,
            [0.1, 0.2, 0.3],
            tenant_id=TENANT,
            index_versions={KB: "v1"},
            top_k=5,
        )
    )

    assert rows == []
    sql = _sql(session.statement)
    assert "JOIN documents ON documents.id = chunks.document_id" in sql, (
        "当前稿指针在文档行上：不 JOIN 就没有“谁是当前稿”这个事实"
    )
    assert "chunks.doc_version = documents.current_version" in sql, (
        "少了这一条，旧稿切片会以同等分数参与 TopK 竞争（ADR-0005 决策：默认只查当前稿）"
    )


def test_single_kb_vector_recall_uses_the_same_current_draft_predicate() -> None:
    """单库版召回（``similarity_search``）与范围版同一个口径：两条 SQL 不能各写一套。"""

    session = _CapturingSession()

    asyncio.run(
        PgVectorStore().similarity_search(
            session,
            [0.1, 0.2, 0.3],
            tenant_id=TENANT,
            kb_id=KB,
            top_k=5,
        )
    )

    sql = _sql(session.statement)
    assert "chunks.doc_version = documents.current_version" in sql
