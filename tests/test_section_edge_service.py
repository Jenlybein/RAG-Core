"""#152 · 关系边的接线层：先删后写、幂等、清悬空、作用域与版本。

纯抽取规则在 ``tests/test_section_graph_extraction.py`` 里穷举；这里守的是**接线**：

1. **先删后写**：重建某篇文档时它在本版本里的边（出边与入边）先清掉，再写新的；
2. **幂等**：``INSERT ... ON CONFLICT DO NOTHING`` + 八列身份，同一条边不会变成两条；
3. **不留悬空边**：写完立刻用本版本**真实的**节全集剪一遍——两端至少一端不存在的边
   删掉。验收里「重建同一文档后边仍指向存在的节」就是这两条一起成立的推论：
   写进去的边两端都在目录里（用例 6），而目录外的边被剪掉（用例 3/5）；
4. **作用域**：边落在哪一版由 ``EdgeScope`` 一处决定，读目录的查询带齐租户 / 库 / 版本；
5. **索引期是唯一建边时点**：``IndexingService`` 在「切片写完、状态回写之前」建边，
   版本就是这次写入的目标版本；整库构建在逐篇重建之后再整版重算一遍（跨文档引用）。

离线：会话是记录型替身（``tests/fake_db``），语句形状用 PostgreSQL 方言编译后断言
——``ON CONFLICT`` 与行值 ``NOT IN`` 只有编译出来才看得见。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models.section_edge import UNIQUE_EDGE_COLUMNS, SectionEdge
from app.models.chunk import Chunk
from app.models.document import Document
from app.repositories import section_edge_repository
from app.services import index_build_service, indexing_service
from app.services.section_edge_service import (
    rebuild_document_edges,
    rebuild_version_edges,
)
from app.services.section_graph import EdgeScope, SectionRecord, SectionText
from app.utils.section_identity import section_id_of
from app.utils.status import DocumentStatus
from tests.fake_db import AsyncRecordingSession

TENANT = "tenant-a"
KB = "kb-1"
VERSION = "v2"
SCOPE = EdgeScope(tenant_id=TENANT, kb_id=KB, index_version=VERSION)


# ─────────────────────────────────────────────────────────── 构造器


def _sql(statement: Any, *, literal: bool = True) -> str:
    """按 PostgreSQL 方言编译成一条可读的 SQL（``ON CONFLICT`` 只在那个方言里出现）。

    ``literal=False`` 用于带 JSONB 证据列的 INSERT：那个类型没有字面渲染器，参数会留在
    绑定位里——本文件只关心语句的**形状**，值由抽取层用用例钉住。
    """

    options = {"literal_binds": True} if literal else {}
    compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs=options)
    return " ".join(str(compiled).split())


def _chunk_row(
    *,
    document_id: str,
    title: str,
    heading: str,
    chunk_index: int,
    content: str,
    index_version: str = VERSION,
) -> SimpleNamespace:
    """一行切片的替身（``list_version_sections`` 读的就是这四列 + meta）。"""

    return SimpleNamespace(
        id=f"chunk-{document_id}-{chunk_index}",
        tenant_id=TENANT,
        kb_id=KB,
        index_version=index_version,
        document_id=document_id,
        content=content,
        meta={
            "title": title,
            "section_id": section_id_of(document_id=document_id, heading_path=heading),
            "heading_path": heading,
            "chunk_index": chunk_index,
        },
    )


def _session(*rows: SimpleNamespace) -> AsyncRecordingSession:
    return AsyncRecordingSession(rows={Chunk: list(rows)})


def _section(*, document_id: str, title: str, heading: str, order: int, text: str) -> SectionText:
    return SectionText(
        record=SectionRecord(
            tenant_id=TENANT,
            kb_id=KB,
            index_version=VERSION,
            document_id=document_id,
            document_title=title,
            section_id=section_id_of(document_id=document_id, heading_path=heading),
            heading_path=heading,
            order=order,
        ),
        text=text,
    )


# ────────────────────────────────────────────── 语句形状：幂等与清悬空


def test_the_insert_is_idempotent_on_the_eight_column_identity() -> None:
    """同一条边再抽一次是同一条边：唯一冲突时什么都不做，不报错、不覆盖。"""

    draft = section_edge_repository.build_insert_statement(
        [
            SimpleNamespace(
                tenant_id=TENANT,
                kb_id=KB,
                index_version=VERSION,
                source_document_id="doc-a",
                source_section_id="s1",
                target_document_id="doc-b",
                target_section_id="s2",
                relation_type="reference",
                metadata={"expression": "《促销规则》2.4 节"},
            )
        ]
    )

    sql = _sql(draft, literal=False)
    assert "INSERT INTO section_edges" in sql
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    for column in UNIQUE_EDGE_COLUMNS:
        assert column in sql


def test_deleting_a_documents_edges_covers_both_directions_and_the_scope() -> None:
    """重建一篇：它作为起点与终点的边都要删，且只在**这一版**里删。"""

    sql = _sql(section_edge_repository.build_delete_for_document_statement(SCOPE, "doc-a"))

    assert sql.startswith("DELETE FROM section_edges")
    assert "tenant_id" in sql and "kb_id" in sql and "index_version" in sql
    assert "source_document_id = 'doc-a'" in sql
    assert "target_document_id = 'doc-a'" in sql


def test_pruning_removes_edges_whose_ends_are_gone_from_the_version() -> None:
    """剪悬空：两端都必须在「本版本真实存在的节」里，否则这条边就该消失。"""

    sql = _sql(
        section_edge_repository.build_prune_statement(SCOPE, [("doc-a", "s1"), ("doc-b", "s2")])
    )

    assert sql.startswith("DELETE FROM section_edges")
    assert "(section_edges.source_document_id, section_edges.source_section_id)" in sql
    assert "(section_edges.target_document_id, section_edges.target_section_id)" in sql
    assert "NOT IN" in sql
    assert "('doc-a', 's1')" in sql and "('doc-b', 's2')" in sql


def test_pruning_with_no_sections_clears_the_whole_version() -> None:
    """一版本里一个节都没有：没有一条边是合法的（这比留下整版悬空边诚实）。"""

    sql = _sql(section_edge_repository.build_prune_statement(SCOPE, []))

    assert sql.startswith("DELETE FROM section_edges")
    assert "NOT IN" not in sql, "没有合法节对时不该拼一个空 IN 列表（那是语法错）"
    assert "index_version" in sql


# ───────────────────────────────────────────────────── 重建：先删、再建、剪悬空


def test_rebuilding_deletes_before_inserting_and_prunes_after() -> None:
    """三步的顺序是契约：先删旧边 → 写新边 → 剪悬空。"""

    session = _session(
        _chunk_row(
            document_id="doc-a",
            title="售后手册",
            heading="2.1 退款",
            chunk_index=0,
            content="提交申请。",
        ),
        _chunk_row(
            document_id="doc-a",
            title="售后手册",
            heading="2.2 审核",
            chunk_index=1,
            content="人工审核。",
        ),
    )

    asyncio.run(
        rebuild_document_edges(
            session,
            scope=SCOPE,
            document_id="doc-a",
            document_title="售后手册",
            sections=(
                _section(
                    document_id="doc-a",
                    title="售后手册",
                    heading="2.1 退款",
                    order=0,
                    text="提交申请。",
                ),
                _section(
                    document_id="doc-a",
                    title="售后手册",
                    heading="2.2 审核",
                    order=1,
                    text="人工审核。",
                ),
            ),
        )
    )

    verbs = [statement.strip().split(maxsplit=1)[0].upper() for statement in session.statements]
    delete_index = next(i for i, verb in enumerate(verbs) if verb == "DELETE")
    insert_index = next(i for i, verb in enumerate(verbs) if verb == "INSERT")
    # 最后一条 DELETE 是剪悬空（第一条是删该文档的边）。
    assert delete_index < insert_index < len(verbs) - 1
    assert verbs[-1] == "DELETE", "剪悬空必须是最后一步（新边写完才判得准）"
    assert "section_edges" in session.statements[-1]


def test_written_edges_never_point_outside_the_version_catalog() -> None:
    """写进去的每一条边，两端都在本版本目录里——「没有悬空边」的前一半。"""

    session = _session(
        _chunk_row(
            document_id="doc-a",
            title="售后手册",
            heading="2.1 退款",
            chunk_index=0,
            content="详见《促销规则》2.4 节。",
        ),
        _chunk_row(
            document_id="doc-b",
            title="促销规则",
            heading="2.4 到账时限",
            chunk_index=0,
            content="三个工作日内到账。",
        ),
    )

    result = asyncio.run(
        rebuild_document_edges(
            session,
            scope=SCOPE,
            document_id="doc-a",
            document_title="售后手册",
            sections=(
                _section(
                    document_id="doc-a",
                    title="售后手册",
                    heading="2.1 退款",
                    order=0,
                    text="详见《促销规则》2.4 节。",
                ),
            ),
        )
    )

    catalog_pairs = {
        ("doc-a", section_id_of(document_id="doc-a", heading_path="2.1 退款")),
        ("doc-b", section_id_of(document_id="doc-b", heading_path="2.4 到账时限")),
    }
    assert result.count == 1, "这一句只该抽出一条跨文档引用边"
    for edge in result.edges:
        assert (edge.source_document_id, edge.source_section_id) in catalog_pairs
        assert (edge.target_document_id, edge.target_section_id) in catalog_pairs
        assert (edge.tenant_id, edge.kb_id, edge.index_version) == (TENANT, KB, VERSION)


def test_the_version_pass_resolves_a_reference_whose_target_was_written_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整库收尾那一遍：目录完整了，先写的那篇指向后写的篇也能建出边。"""

    session = _session(
        _chunk_row(
            document_id="doc-a",
            title="售后手册",
            heading="2.1 退款",
            chunk_index=0,
            content="详见《促销规则》2.4 节。",
        ),
        _chunk_row(
            document_id="doc-b",
            title="促销规则",
            heading="2.4 到账时限",
            chunk_index=0,
            content="三个工作日内到账。",
        ),
    )
    inserted: list[Any] = []

    async def _record_insert_many(_db: Any, drafts: Any) -> None:
        inserted.extend(drafts)

    monkeypatch.setattr(section_edge_repository, "insert_many", _record_insert_many)

    total = asyncio.run(rebuild_version_edges(session, scope=SCOPE))

    assert total == len(inserted) == 1
    edge = inserted[0]
    assert (edge.source_document_id, edge.target_document_id) == ("doc-a", "doc-b"), (
        "doc-b 是后写的那篇：整版重算必须把它补进引用边"
    )


def test_the_version_pass_only_reads_its_own_version() -> None:
    """别的版本的切片不在目录里，于是跨版本的边一条都建不出来。"""

    session = _session(
        _chunk_row(
            document_id="doc-a",
            title="售后手册",
            heading="2.1 退款",
            chunk_index=0,
            content="详见《促销规则》2.4 节。",
        ),
        _chunk_row(
            document_id="doc-b",
            title="促销规则",
            heading="2.4 到账时限",
            chunk_index=0,
            content="三个工作日内到账。",
            index_version="v1",
        ),
    )

    asyncio.run(rebuild_version_edges(session, scope=SCOPE))

    # 只有 doc-a 自己的边：sibling / parent 都为 0，指向 doc-b 的引用边也不该有。
    inserts = [statement for statement in session.statements if statement.startswith("INSERT")]
    assert inserts == [], "v1 的目标节读不到，引用边不该被建出来"


# ────────────────────────────────────────────── 索引期：版本与时机


class _RecordingEdgeBuilder:
    """建边替身：记下被调用的顺序、作用域与切片数。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.scopes: list[EdgeScope] = []
        self.sections: list[tuple[str, int]] = []

    async def rebuild_document(
        self,
        _db: Any,
        *,
        scope: EdgeScope,
        document_id: str,
        document_title: str,
        sections: Any,
    ) -> Any:
        self.calls.append("edges")
        self.scopes.append(scope)
        self.sections.append((document_id, len(sections)))
        assert document_title
        return None


class _RecordingStore:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def delete_by_document_id(
        self, _db: Any, _document_id: str, *, tenant_id: str, index_version: str | None = None
    ) -> None:
        self.calls.append("purge")

    async def add_chunks(self, _db: Any, _chunks: list[dict[str, Any]]) -> None:
        self.calls.append("add")


class _StubEmbeddings:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _StubSplitter:
    def __init__(self, pieces: list[Any]) -> None:
        self.pieces = pieces

    def split(self, _text: str) -> list[Any]:
        return self.pieces


class _Session:
    """索引编排用的异步会话替身：只数提交次数。"""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: Any) -> Any:
        raise AssertionError("建边已被替身接管，会话不该收到查询")


def _piece(text: str, heading: str, *, document_id: str = "doc-a") -> Any:
    from app.utils.text_splitter import SplitPiece

    return SplitPiece(
        text=text,
        metadata={
            "heading_path": heading,
            "section_id": section_id_of(document_id=document_id, heading_path=heading),
        },
    )


def test_indexing_builds_edges_in_the_target_version_after_the_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引期建边：在「切片写完、状态回写之前」，版本就是这次写入的目标版本。"""

    calls: list[str] = []
    pieces = [_piece("提交申请。", "2 退款 > 2.1 申请")]
    builder = _RecordingEdgeBuilder(calls)
    document = SimpleNamespace(
        id="doc-a",
        tenant_id=TENANT,
        kb_id=KB,
        title="售后手册",
        content="raw",
        doc_version=1,
        source_filename="售后手册.md",
    )

    async def _get_by_id(_db: Any, _id: str) -> Any:
        return document

    async def _claim(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _update_status(*_args: Any, **_kwargs: Any) -> None:
        calls.append("status")

    monkeypatch.setattr(indexing_service.document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(
        indexing_service.document_repository, "try_claim_for_indexing_async", _claim
    )
    monkeypatch.setattr(indexing_service.document_repository, "update_status", _update_status)
    monkeypatch.setattr(
        indexing_service,
        "prepare_document_content",
        lambda _doc, *, reparse=False: SimpleNamespace(
            content="raw", source_type="markdown", metadata={}
        ),
    )

    service = indexing_service.IndexingService(
        splitter=_StubSplitter(pieces),
        embeddings=_StubEmbeddings(),
        stores=[_RecordingStore(calls)],
        edge_builder=builder,
    )

    asyncio.run(service.index_existing_document(_Session(), "doc-a", index_version="v2"))

    assert calls == ["purge", "add", "edges", "status"], (
        "建边必须夹在「切片写完」与「状态回写」之间（同一笔事务）"
    )
    assert [(scope.tenant_id, scope.kb_id, scope.index_version) for scope in builder.scopes] == [
        (TENANT, KB, "v2")
    ], "边必须落在这次写入的目标版本上，不是写死的 v1"
    assert builder.sections == [("doc-a", 1)]


# ────────────────────────────────────────────── 整库构建：收尾再算一遍


def test_the_build_recomputes_the_whole_version_after_indexing_the_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整库构建：逐篇重建之后整版重算一次（跨文档引用要等目录完整才算得出来）。"""

    recorded: list[tuple[str, str, str]] = []

    async def _rebuild_version_edges(_db: Any, *, scope: EdgeScope) -> int:
        recorded.append((scope.tenant_id, scope.kb_id, scope.index_version))
        return 0

    monkeypatch.setattr(index_build_service, "rebuild_version_edges", _rebuild_version_edges)

    class _Indexing:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def index_existing_document(
            self, _db: Any, document_id: str, *, index_version: str | None = None, **_kwargs: Any
        ) -> int:
            self.calls.append(f"{document_id}:{index_version}")
            return 1

    indexing = _Indexing()
    service = index_build_service.IndexBuildService(
        indexing=indexing,
        vector_store=SimpleNamespace(count_by_scope=_zero_count),
        keyword_search=SimpleNamespace(available=False),
    )

    row = SimpleNamespace(
        id="build-1",
        tenant_id=TENANT,
        kb_id=KB,
        version=VERSION,
        status="building",
        document_count=None,
        chunk_count=None,
        error_message=None,
        created_at=None,
        activated_at=None,
        archived_at=None,
    )
    kb = SimpleNamespace(id=KB, tenant_id=TENANT, active_index_version=VERSION)
    documents = [
        SimpleNamespace(
            id="doc-a",
            tenant_id=TENANT,
            kb_id=KB,
            title="售后手册",
            doc_version=1,
            status=DocumentStatus.SUCCESS,
            content="正文",
        )
    ]
    session = AsyncRecordingSession(
        rows={
            index_build_service.IndexBuildVersion: [row],
            index_build_service.KnowledgeBase: [kb],
            Document: documents,
        }
    )

    asyncio.run(service.build(session, "build-1", tenant_id=TENANT))

    assert indexing.calls == ["doc-a:v2"], "先逐篇重建到目标版本"
    assert recorded == [(TENANT, KB, VERSION)], "收尾整版重算一次，版本是构建的目标版本"


async def _zero_count(*_args: Any, **_kwargs: Any) -> int:
    return 0


def test_the_edge_table_is_the_only_new_storage() -> None:
    """本票不往既有表加列：关系边只落在 section_edges 一张表里。"""

    assert SectionEdge.__tablename__ == "section_edges"
    assert not hasattr(SectionEdge, "embedding")


def test_the_prune_predicate_is_a_row_value_comparison() -> None:
    """剪悬空用的是行值 ``(文档, 节)``，不是两个独立 ``IN``——后者会少删也会多删。"""

    statement = section_edge_repository.build_prune_statement(SCOPE, [("doc-a", "s1")])
    rendered = str(statement.whereclause)

    assert "(section_edges.source_document_id, section_edges.source_section_id)" in rendered
    assert "(section_edges.target_document_id, section_edges.target_section_id)" in rendered
