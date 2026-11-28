"""#179 · 文档版本对账：四条不变量 + 关键词那一维的「不可判定」。

父 spec D14 把对账拆成**三条库内不变量 + 一条跨系统**。每一条回答的问题是「哪两处真值
本该相等、其实不等」：

1. 每个切片的稿号都能找到对应的版本行（切片指向一稿，版本表里必须真有那一稿）；
2. 每篇文档**恰好一个**当前稿版本行，且它的稿号等于文档上的指针（「单活」不成立时，
   后面两条都在拿一份不知道是哪一稿的正文做比较）；
3. 文档正文（`documents.content`）与当前稿正文逐字相同（D6 的代价：快照与真值分两处存）；
4. 关键词索引里标着「当前稿」的切片，稿号等于文档指针（跨系统：ES 不在任何事务里）。

**第四条的两种失败必须分开**：「关键词能力不存在」（这台机器没有引擎）不是数据分歧，
记**不可判定**、不判失败；「能力在、这一次读不到」同样不可判定，但原因要写出来。
把它记成「通过」等于撒谎，记成「失败」等于让一次没部署 ES 的机器永远红。

本文件从下往上分四段，每段一个接缝：

* ① 纯判据（`app.services.document_version_reconcile`）：给定一篇文档的读数 → 分歧清单；
* ② 读路径（三个仓储）：真 SQL（`sqlite+aiosqlite`）上把读数取对；
* ③ 关键词读能力（`KeywordSearchProvider.current_draft_versions`）：假客户端，看查询体；
* ④ 运维脚本（`scripts/verify_document_versions.py`）：退出码、`--kb-id` / `--limit`、
  不可判定的那一行打印、以及**只读**（一条写语句、一次提交都没有）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.plan import Plan
from app.models.tenant import Tenant
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.providers.keyword_search.query_builder import (
    CURRENT_DRAFT_FIELD,
    DOC_VERSION_FIELD,
    build_current_draft_versions_query,
)
from app.repositories import chunk_repository, document_repository, document_version_repository
from app.services import document_version_reconcile as reconcile_module
from app.services.document_version_reconcile import (
    KEYWORD_REASON_ABSENT,
    KEYWORD_REASON_FAILED,
    KEYWORD_STATE_CHECKED,
    KEYWORD_STATE_UNDECIDABLE,
    DocumentFacts,
    VersionRow,
)

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _facts(
    *,
    document_id: str = "doc-1",
    pointer: int = 2,
    content: str = "当前稿正文。",
    version_rows: Sequence[tuple[int, str, str]] = (
        (1, "archived", "旧稿正文。"),
        (2, "current", "当前稿正文。"),
    ),
    chunk_versions: Sequence[tuple[int, int]] = ((2, 3),),
    keyword_current_versions: Sequence[int] | None = (2,),
) -> DocumentFacts:
    """一篇文档的对账读数。

    `chunk_versions` 是 ``(稿号, 这一稿有几片)`` 的列表（仓储就是按这对值分组的），
    `keyword_current_versions` 是关键词索引里标着「当前稿」的稿号集合（``None`` = 不可判定）。

    `version_rows` 用三个一组的字面量写最省事（``(稿号, 状态, 正文)``），但**读数本身是
    :class:`VersionRow`**——判据靠字段名取值，不靠位置：``row[1]`` 在那种写法里既可能是
    稿号也可能是状态，写反了类型仍然对。
    """

    return DocumentFacts(
        document_id=document_id,
        pointer=pointer,
        content=content,
        version_rows=tuple(VersionRow(*row) for row in version_rows),
        chunk_versions=tuple(chunk_versions),
        keyword_current_versions=(
            None if keyword_current_versions is None else tuple(keyword_current_versions)
        ),
    )


def _report(*facts: DocumentFacts, keyword_state: str = KEYWORD_STATE_CHECKED, reason=None):
    return reconcile_module.reconcile(facts, keyword_state=keyword_state, keyword_reason=reason)


# ────────────────────────────── ① 检查 1：每个切片的稿号都能找到版本行


def test_a_chunk_pointing_at_a_draft_without_a_version_row_is_reported() -> None:
    """切片说「我属于第 5 稿」，而版本表里只有 1、2——那一片属于一稿谁也读不到。"""

    report = _report(_facts(chunk_versions=((2, 3), (5, 2))))

    assert len(report.chunk_version_rows) == 1
    discrepancy = report.chunk_version_rows[0]
    assert discrepancy.document_id == "doc-1"
    assert "5" in discrepancy.detail, "要点名是哪个稿号没有版本行"
    assert report.has_disagreements is True


def test_a_version_row_with_no_chunks_is_not_a_disagreement() -> None:
    """反方向不判错：某一稿今天没有切片是合法的（保留窗口 / 重建中途 / 空文档）。"""

    report = _report(_facts(chunk_versions=((2, 3),)))

    assert report.chunk_version_rows == ()
    assert report.has_disagreements is False


# ───────────── ② 检查 2：恰好一个当前稿，且稿号等于文档上的指针（「单活」）


def test_a_document_with_two_current_drafts_is_reported() -> None:
    """两条当前稿 = 指针之外的「另一个真值」也在生效：单活破了。"""

    report = _report(
        _facts(version_rows=((1, "archived", "旧"), (2, "current", "甲"), (3, "current", "乙")))
    )

    assert len(report.current_drafts) == 1
    detail = report.current_drafts[0].detail
    assert "2" in detail and "3" in detail, "两个稿号都要列出来"
    assert "1" in detail or "恰好" in detail, "要说清期望是恰好一条"


def test_a_current_draft_that_is_not_the_pointer_is_reported() -> None:
    """唯一那条当前稿不是指针指的那一稿：检索（按指针）与对账（按状态）会对同一篇给出两个答案。"""

    report = _report(_facts(pointer=2, version_rows=((1, "current", "旧"), (2, "archived", "新"))))

    assert len(report.current_drafts) == 1
    detail = report.current_drafts[0].detail
    assert "1" in detail and "2" in detail, "当前稿与指针两个值都要出现"


def test_a_document_without_any_current_draft_is_reported() -> None:
    """一条当前稿都没有：指针指着一稿版本表里没有（或被保留策略删掉了）。"""

    report = _report(_facts(version_rows=((1, "archived", "旧"),)))

    assert len(report.current_drafts) == 1
    assert "doc-1" == report.current_drafts[0].document_id


# ───────────────────── ③ 检查 3：文档正文与当前稿正文逐字相同（快照 vs 真值）


def test_a_body_that_is_not_byte_for_byte_the_current_draft_is_reported() -> None:
    """`documents.content` 是**快照**，版本行是**真值**（D6）：两者不等就是一条分歧。"""

    report = _report(_facts(content="当前稿正文"))  # 少一个句号

    assert len(report.content_snapshots) == 1
    detail = report.content_snapshots[0].detail
    assert "documents.content" in detail or "文档正文" in detail
    assert "字符" in detail, "给出两边的长度，运维据此判断差多少"


def test_the_body_comparison_is_verbatim_not_trimmed() -> None:
    """逐字比较：首尾空白与换行也算差异（规范化只在写入时做一次，读出来就要一样）。"""

    report = _report(_facts(content="当前稿正文。\n"))

    assert len(report.content_snapshots) == 1


def test_a_document_whose_single_current_draft_is_missing_is_not_counted_again() -> None:
    """「当前稿不唯一 / 不存在」只记在检查 2：同一条根因不在两个计数里各报一次。"""

    report = _report(_facts(version_rows=((1, "archived", "旧"),), content="随便什么"))

    assert len(report.current_drafts) == 1
    assert report.content_snapshots == ()


# ───────────────── ④ 检查 4：关键词索引里「当前稿」的稿号等于文档指针


def test_keyword_current_chunks_pointing_at_another_draft_are_reported() -> None:
    """关键词索引用一个布尔表达「当前稿」（#174）：布尔翻错了，两路就对同一篇给出两稿。"""

    report = _report(_facts(pointer=2, keyword_current_versions=(1,)))

    assert len(report.keyword_current_drafts) == 1
    detail = report.keyword_current_drafts[0].detail
    assert "1" in detail and "2" in detail


def test_the_keyword_dimension_is_not_a_failure_when_the_capability_is_absent() -> None:
    """**能力不存在 ≠ 数据缺失**：这一维记「不可判定」，其余三条照常判。

    「没部署 ES」这台机器上，第四条永远得不出答案；把它记成失败会让对账脚本在这种
    部署里一直红（然后被人加 `|| true`），记成通过则是撒谎。所以它是第三态。
    """

    report = _report(
        _facts(keyword_current_versions=None),
        keyword_state=KEYWORD_STATE_UNDECIDABLE,
        reason=KEYWORD_REASON_ABSENT,
    )

    assert report.keyword_state == KEYWORD_STATE_UNDECIDABLE
    assert report.keyword_reason == KEYWORD_REASON_ABSENT
    assert report.keyword_current_drafts == ()
    assert report.has_disagreements is False, "不可判定不是分歧"
    assert report.disagreements == 0


def test_a_keyword_read_that_fails_is_undecidable_with_its_own_reason() -> None:
    """「能力在、这次读不到」是另一种不可判定：原因要能分开（一个要装引擎，一个要排障）。"""

    report = _report(
        _facts(keyword_current_versions=None),
        keyword_state=KEYWORD_STATE_UNDECIDABLE,
        reason=KEYWORD_REASON_FAILED,
    )

    assert report.keyword_reason != KEYWORD_REASON_ABSENT
    assert report.has_disagreements is False


def test_a_checked_keyword_dimension_with_no_current_draft_is_an_information_only() -> None:
    """查得到、但一篇都没标「当前稿」：**信息项**，不是分歧（老文档缺字段 / 还没写进去）。

    `build_mapping_update` 的注释把这件事判给对账脚本（「由对账脚本而不是检索来管」）：
    它不判失败，但必须被看见——否则「关键词路静默全空」只会在一线被当成「没命中」。
    """

    report = _report(_facts(keyword_current_versions=()))

    assert report.keyword_current_drafts == ()
    assert len(report.keyword_drafts_without_current) == 1
    assert report.has_disagreements is False


# ─────────────────────────────────────────── 报告：计数、顺序与「在途窗口」信息项


def test_the_report_counts_the_four_checks_in_a_fixed_order() -> None:
    """四条检查的**顺序与名字**是打印契约：运维按它读，用例按它断言。"""

    report = _report(_facts())

    assert [name for name, _ in report.checks] == [
        reconcile_module.CHECK_CHUNK_VERSION_ROWS,
        reconcile_module.CHECK_CURRENT_DRAFT,
        reconcile_module.CHECK_CONTENT_SNAPSHOT,
        reconcile_module.CHECK_KEYWORD_CURRENT_DRAFT,
    ]
    assert report.documents == 1
    assert report.disagreements == 0
    assert report.has_disagreements is False


def test_a_pointer_draft_without_chunks_is_reported_as_information_only() -> None:
    """「指针那一稿没有切片」：D5 的**在途窗口**信号——重建没跑完与真的丢了在这里分开看。

    它不判失败：切稿与重建之间本来就有一段时间查不到（D5 明确接受这个空窗），把这些
    文档记成分歧会让每次切稿后的对账都红一次。
    """

    report = _report(
        _facts(
            pointer=3,
            content="第 3 稿正文。",
            version_rows=((2, "archived", "旧稿正文。"), (3, "current", "第 3 稿正文。")),
            chunk_versions=((3, 0), (2, 5)),
            keyword_current_versions=(3,),
        )
    )

    assert len(report.pointer_drafts_without_chunks) == 1
    assert "3" in report.pointer_drafts_without_chunks[0].detail
    assert report.has_disagreements is False


def test_every_disagreement_renders_as_one_operable_line() -> None:
    """逐条列出：一行 = 一篇文档 + 一句话，可以贴进工单。"""

    report = _report(_facts(document_id="doc-9", pointer=2, version_rows=((1, "current", "旧"),)))

    rendered = [discrepancy.render() for discrepancy in report.current_drafts]
    assert rendered and rendered[0].startswith("doc-9")


# ───────────────────────────────────────────── ② 读路径：三条查询（真 SQL）

TENANT = "tenant_demo"
KB = "kb-1"
OTHER_KB = "kb-2"

#: 切片表在 SQLite 上要手写 DDL：``Vector(1536)`` 与 ``JSONB`` 在这里换成 TEXT / JSON
#: （与 ``test_retrieval_current_draft.py``、``test_index_version.py`` 同一套做法）。
CHUNKS_DDL = """
CREATE TABLE chunks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata JSON NOT NULL,
    index_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    doc_version INTEGER NOT NULL DEFAULT '1',
    generation INTEGER NOT NULL DEFAULT 1
)
"""


def _document(
    document_id: str,
    *,
    kb_id: str = KB,
    current_version: int = 1,
    content: str = "正文。",
    tenant_id: str = TENANT,
) -> Document:
    return Document(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        title=document_id,
        content=content,
        source_type="markdown",
        status=3,
        current_version=current_version,
        created_at=datetime(2026, 11, 1),
        updated_at=datetime(2026, 11, 1),
    )


def _version(
    document_id: str, version: int, *, status: str = "current", content: str = "正文。"
) -> DocumentVersion:
    return DocumentVersion(
        id=f"{document_id}-v{version}",
        document_id=document_id,
        version=version,
        content=content,
        checksum="0" * 64,
        status=status,
        created_at=datetime(2026, 11, 1),
    )


def _seeded_documents() -> tuple[Document, ...]:
    """三篇文档：`doc-1` 两稿（指针 2）、`doc-2` 一稿、`doc-3` 在另一个库里。

    **每次调用现造**（不在模块级摆一份）：ORM 对象是有状态的，用例改了它（检查 3 那条就
    改文档行的正文），模块级的那一份会把改动带到下一个用例里。
    """

    return (
        _document("doc-1", current_version=2, content="第 2 稿正文。"),
        _document("doc-2", content="第 1 稿正文。"),
        _document("doc-3", kb_id=OTHER_KB, content="别的库。"),
    )


def _seeded_versions() -> tuple[DocumentVersion, ...]:
    return (
        _version("doc-1", 1, status="archived", content="第 1 稿正文。"),
        _version("doc-1", 2, content="第 2 稿正文。"),
        _version("doc-2", 1, content="第 1 稿正文。"),
        _version("doc-3", 1, content="别的库。"),
    )


#: 切片：`doc-1` 的两稿各有切片（旧稿的按保留策略还在），`doc-2` 一片，`doc-3` 三片。
SEEDED_CHUNKS = (
    ("chunk-1", "doc-1", KB, 1),
    ("chunk-2", "doc-1", KB, 2),
    ("chunk-3", "doc-1", KB, 2),
    ("chunk-4", "doc-2", KB, 1),
    ("chunk-5", "doc-3", OTHER_KB, 1),
    ("chunk-6", "doc-3", OTHER_KB, 1),
    ("chunk-7", "doc-3", OTHER_KB, 1),
)


@pytest_asyncio.fixture
async def reconcile_db() -> AsyncIterator[AsyncSession]:
    """``documents`` / ``document_versions`` / ``chunks`` 三张真表 + 一批种子行。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(DocumentVersion.__table__.create)
        # 租户与套餐（#220）：索引流水线要读租户套餐来判单库 token 容量。
        # 少了这两张，这条用例会在「no such table: tenants」上炸。
        await connection.run_sync(Tenant.__table__.create)
        await connection.run_sync(Plan.__table__.create)
        await connection.execute(text(CHUNKS_DDL))
        for row in SEEDED_CHUNKS:
            await connection.execute(
                text(
                    "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding,"
                    " metadata, index_version, doc_version) VALUES"
                    " (:id, :tenant_id, :kb_id, :document_id, '片正文。', '[]', '{}', 'v1',"
                    " :doc_version)"
                ),
                {
                    "id": row[0],
                    "tenant_id": TENANT,
                    "kb_id": row[2],
                    "document_id": row[1],
                    "doc_version": row[3],
                },
            )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for row in (*_seeded_documents(), *_seeded_versions()):
            session.add(row)
        await session.flush()
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_scan_returns_every_document_and_can_be_narrowed(
    reconcile_db: AsyncSession,
) -> None:
    """默认全量；``--kb-id`` 收窄库、``--limit`` 收敛条数（运维先看一小批再定位）。"""

    every = await document_repository.list_for_version_reconcile(reconcile_db)
    one_kb = await document_repository.list_for_version_reconcile(reconcile_db, kb_id=OTHER_KB)
    capped = await document_repository.list_for_version_reconcile(reconcile_db, limit=2)

    assert {doc.id for doc in every} == {"doc-1", "doc-2", "doc-3"}
    assert [doc.id for doc in one_kb] == ["doc-3"]
    assert len(capped) == 2


@pytest.mark.asyncio
async def test_version_rows_are_read_for_a_batch_of_documents(
    reconcile_db: AsyncSession,
) -> None:
    """一次取一批（不是一篇一次）：`doc-1` 的两稿都在，别的文档的行不混进来。"""

    rows = await document_version_repository.list_for_documents(reconcile_db, ["doc-1", "doc-2"])

    by_document: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        by_document.setdefault(row.document_id, []).append((row.version, row.status))
    assert by_document == {
        "doc-1": [(1, "archived"), (2, "current")],
        "doc-2": [(1, "current")],
    }


@pytest.mark.asyncio
async def test_draft_counts_group_the_chunks_of_each_document_by_draft(
    reconcile_db: AsyncSession,
) -> None:
    """``(文档, 稿号) → 几片``：检查 1 要的「出现过哪些稿号」，信息项要的「指针那一稿有没有片」。"""

    counts = await chunk_repository.draft_counts(reconcile_db, document_ids=["doc-1", "doc-2"])

    assert counts == [("doc-1", 1, 1), ("doc-1", 2, 2), ("doc-2", 1, 1)]
    assert await chunk_repository.draft_counts(reconcile_db, document_ids=[]) == []


@pytest.mark.asyncio
async def test_the_reads_never_touch_a_column_the_model_does_not_declare(
    reconcile_db: AsyncSession,
) -> None:
    """三条读语句都只用真实列（``chunks.doc_version`` 是 #172 的列，不是 JSONB 里的键）。"""

    from sqlalchemy.dialects import postgresql

    statement = chunk_repository._draft_counts_statement(document_ids=["doc-1"])
    sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())

    assert "chunks.doc_version" in sql
    assert "chunks.document_id" in sql
    assert "group by" in sql.lower()


# ───────────────────────────── ③ 关键词那一维：provider 的只读能力（跨系统）


def test_the_keyword_read_asks_only_this_tenants_current_draft_chunks() -> None:
    """三个过滤条件都不能省：租户（ADR-0006）、``is_current``（#174）、这批文档。"""

    payload = build_current_draft_versions_query(tenant_id=TENANT, document_ids=["doc-1"])

    assert payload["size"] == 0, "只要聚合结果，不要命中行（文档多的时候那是几百 KB 正文）"
    filters = payload["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": TENANT}} in filters
    assert {"term": {CURRENT_DRAFT_FIELD: True}} in filters
    assert {"terms": {"document_id": ["doc-1"]}} in filters
    assert DOC_VERSION_FIELD in payload["aggs"]["documents"]["aggs"]["drafts"]["terms"]["field"]


class _FakeSearchClient:
    """只实现 ``search`` 的假客户端：记下实参，回一份可编排的响应。"""

    def __init__(self, response: dict | None = None, *, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or {}
        self._error = error

    async def search(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _es_provider(client: Any) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://es.example:9200", index_name="rag_chunks", client=client
    )


def _buckets(*pairs: tuple[str, Sequence[int]]) -> dict:
    return {
        "aggregations": {
            "documents": {
                "buckets": [
                    {
                        "key": document_id,
                        # mapping 里 ``doc_version`` 是 keyword：桶的 key 是**字符串**。
                        "drafts": {"buckets": [{"key": str(version)} for version in versions]},
                    }
                    for document_id, versions in pairs
                ]
            }
        }
    }


def test_the_provider_returns_the_drafts_marked_current_per_document() -> None:
    """``{document_id: [稿号…]}``：一篇正常只有一个，多于一个是分歧的现场（由判据判）。"""

    client = _FakeSearchClient(_buckets(("doc-1", [2]), ("doc-2", [1])))

    versions = asyncio.run(
        _es_provider(client).current_draft_versions(
            tenant_id=TENANT, document_ids=["doc-1", "doc-2"]
        )
    )

    assert versions == {"doc-1": [2], "doc-2": [1]}
    assert client.calls[0]["index"] == "rag_chunks", "查的是**别名**（线上那一份）"


def test_a_document_outside_the_aggregation_is_absent_from_the_answer() -> None:
    """一条「当前稿」切片都没标的文档**不出现在**答案里（判据按「空」解释它）。"""

    client = _FakeSearchClient(_buckets(("doc-1", [2])))

    versions = asyncio.run(
        _es_provider(client).current_draft_versions(
            tenant_id=TENANT, document_ids=["doc-1", "doc-2"]
        )
    )

    assert versions == {"doc-1": [2]}
    assert "doc-2" not in versions


def test_an_empty_batch_does_not_send_a_query() -> None:
    """空批次不发语句：``--limit 0`` 或空库不该换来一条 ``terms: []`` 的查询。"""

    client = _FakeSearchClient(_buckets())

    assert (
        asyncio.run(_es_provider(client).current_draft_versions(tenant_id=TENANT, document_ids=[]))
        == {}
    )
    assert client.calls == []


def test_a_missing_alias_is_an_honest_empty_answer() -> None:
    """索引还没建 = 关键词库里什么都没有（不是故障）：回空，判据按「没标」记信息项。"""

    from elasticsearch import NotFoundError

    client = _FakeSearchClient(error=NotFoundError(404, "no such index", {}))

    assert (
        asyncio.run(
            _es_provider(client).current_draft_versions(tenant_id=TENANT, document_ids=["doc-1"])
        )
        == {}
    )


def test_a_failing_read_is_raised_so_the_caller_can_call_it_undecidable() -> None:
    """读不到就抛（不在这里吞）：调用方要把「能力在、这次读不到」与「能力不存在」分开打印。"""

    client = _FakeSearchClient(error=RuntimeError("cluster is down"))

    with pytest.raises(RuntimeError, match="cluster is down"):
        asyncio.run(
            _es_provider(client).current_draft_versions(tenant_id=TENANT, document_ids=["doc-1"])
        )


def test_the_noop_provider_declares_the_capability_absent() -> None:
    """空实现：能力不存在 → 这一维不可判定。它照旧实现方法（形状一致），但先看 ``available``。"""

    provider = NoopKeywordSearchProvider()

    assert provider.available is False
    assert (
        asyncio.run(provider.current_draft_versions(tenant_id=TENANT, document_ids=["doc-1"])) == {}
    )


# ───────────────────────────────────────── ④ 运维脚本：取数、打印、退出码


def _load_script() -> ModuleType:
    """从文件加载脚本（``scripts*`` 不在安装包里）：加载本身也要成功——导入即安全。"""

    path = _SCRIPTS_DIR / "verify_document_versions.py"
    spec = importlib.util.spec_from_file_location("t179_verify_document_versions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture
def verify_script() -> ModuleType:
    return _load_script()


class _StubKeywordProvider:
    """关键词实现替身：``available`` 可编排，答案或异常二选一（对账只读，没有写入面）。"""

    def __init__(
        self,
        versions: dict[str, Sequence[int]] | None = None,
        *,
        available: bool = True,
        error: BaseException | None = None,
    ) -> None:
        self.available = available
        self._versions = versions or {}
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def current_draft_versions(
        self, *, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, list[int]]:
        self.calls.append({"tenant_id": tenant_id, "document_ids": list(document_ids)})
        if self._error is not None:
            raise self._error
        return {key: list(value) for key, value in self._versions.items()}


@pytest.mark.asyncio
async def test_a_consistent_database_reports_no_disagreement(
    reconcile_db: AsyncSession,
) -> None:
    """种子数据是自洽的：四条零分歧；关键词那一维因为空实现而**不可判定**（不是失败）。"""

    report = await _reconcile(reconcile_db, provider=NoopKeywordSearchProvider())

    assert (report.documents, report.chunks) == (3, 7)
    assert report.disagreements == 0
    assert report.keyword_state == KEYWORD_STATE_UNDECIDABLE
    assert report.keyword_reason == KEYWORD_REASON_ABSENT


@pytest.mark.asyncio
async def test_each_of_the_four_checks_fires_on_its_own_kind_of_drift(
    reconcile_db: AsyncSession,
) -> None:
    """四种漂移各造一处，四条检查**各报各的**（互不吞并，也不互相掩盖）。"""

    # 检查 1：一片切片声称属于第 9 稿，而版本表里没有那一稿。
    await reconcile_db.execute(
        text(
            "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, metadata,"
            " index_version, doc_version) VALUES ('chunk-orphan', :tenant, :kb, 'doc-2', '孤儿片。',"
            " '[]', '{}', 'v1', 9)"
        ),
        {"tenant": TENANT, "kb": KB},
    )
    # 检查 2：`doc-2` 多出一条当前稿版本行（单活破了）。
    await reconcile_db.execute(
        text(
            "INSERT INTO document_versions (id, document_id, version, content, source_file_path,"
            " checksum, status, created_at) VALUES ('doc-2-v2', 'doc-2', 2, '第 2 稿正文。', NULL,"
            " :checksum, 'current', '2026-11-01 00:00:00')"
        ),
        {"checksum": "0" * 64},
    )
    # 检查 3：`doc-3` 的正文快照与当前稿真值不一致（改的是**文档行**，真值不动）。
    drifted = await reconcile_db.get(Document, "doc-3")
    assert drifted is not None
    drifted.content = "被改过的快照。"
    # 检查 4：关键词索引说 `doc-1` 的当前稿是第 1 稿（指针是 2）。
    provider = _StubKeywordProvider({"doc-1": [1]})

    report = await _reconcile(reconcile_db, provider=provider)

    assert [discrepancy.document_id for discrepancy in report.chunk_version_rows] == ["doc-2"]
    assert [discrepancy.document_id for discrepancy in report.current_drafts] == ["doc-2"]
    assert [discrepancy.document_id for discrepancy in report.content_snapshots] == ["doc-3"]
    assert [discrepancy.document_id for discrepancy in report.keyword_current_drafts] == ["doc-1"]
    assert report.disagreements == 4


@pytest.mark.asyncio
async def test_the_scan_can_be_narrowed_by_kb_and_capped(reconcile_db: AsyncSession) -> None:
    """``--kb-id`` 与 ``--limit`` 收窄范围：运维先看一个库 / 一小批，再决定下一步。"""

    one_kb = await _reconcile(reconcile_db, kb_id=OTHER_KB, provider=NoopKeywordSearchProvider())
    capped = await _reconcile(reconcile_db, limit=2, provider=NoopKeywordSearchProvider())

    assert one_kb.documents == 1
    assert capped.documents == 2


@pytest.mark.asyncio
async def test_a_failing_keyword_read_is_undecidable_not_a_failure(
    reconcile_db: AsyncSession,
) -> None:
    """能力在、这一次读不到：**不可判定**，原因与「能力不存在」不同。"""

    provider = _StubKeywordProvider(error=RuntimeError("cluster is down"))

    report = await _reconcile(reconcile_db, provider=provider)

    assert report.keyword_state == KEYWORD_STATE_UNDECIDABLE
    assert report.keyword_reason == KEYWORD_REASON_FAILED
    assert report.disagreements == 0


@pytest.mark.asyncio
async def test_the_keyword_dimension_is_asked_per_tenant(reconcile_db: AsyncSession) -> None:
    """关键词那一维逐租户问（ADR-0006）：一次跨租户查询会把别人的稿号数进来。"""

    provider = _StubKeywordProvider({"doc-1": [2]})

    await _reconcile(reconcile_db, provider=provider)

    assert [call["tenant_id"] for call in provider.calls] == [TENANT], "种子数据只有一个租户"
    assert len(provider.calls[0]["document_ids"]) == 3


@pytest.mark.asyncio
async def test_a_document_the_keyword_index_does_not_know_is_an_information_item(
    reconcile_db: AsyncSession,
) -> None:
    """查得到这批文档、但其中两篇没有任何「当前稿」切片 → **信息项**，不是不可判定。

    混成一个的话，「老物理索引里缺 ``is_current``」会被打印成「关键词能力不可用」，
    于是那句「去补 mapping」的诊断永远不出现（而它正是 #174 留给对账脚本的活）。
    """

    provider = _StubKeywordProvider({"doc-1": [2]})

    report = await _reconcile(reconcile_db, provider=provider)

    assert report.keyword_state == KEYWORD_STATE_CHECKED
    assert report.keyword_current_drafts == (), "没有分歧"
    assert {item.document_id for item in report.keyword_drafts_without_current} == {
        "doc-2",
        "doc-3",
    }
    assert report.has_disagreements is False


@pytest.mark.asyncio
async def test_the_reconcile_is_read_only_and_idempotent(reconcile_db: AsyncSession) -> None:
    """跑两遍结果一模一样，且**一行都没被改**：对账是观察，不是修复。"""

    provider = NoopKeywordSearchProvider()
    first = await _reconcile(reconcile_db, provider=provider)
    second = await _reconcile(reconcile_db, provider=provider)

    assert first == second
    rows = (await reconcile_db.execute(text("SELECT id, content FROM documents ORDER BY id"))).all()
    assert [tuple(row) for row in rows] == [
        ("doc-1", "第 2 稿正文。"),
        ("doc-2", "第 1 稿正文。"),
        ("doc-3", "别的库。"),
    ]


def test_the_script_contains_no_write_path_at_all() -> None:
    """**只读**是一条结构性质：一条写语句、一次提交、一个事务入口都不许出现在这份脚本里。

    比对行为断言更可靠——今天的脚本恰好没写，不代表下一次改动不会顺手加一句
    「顺手修一下」。这条用例让「顺手修」必须先去改测试。
    """

    source = (_SCRIPTS_DIR / "verify_document_versions.py").read_text(encoding="utf-8")

    for banned in (".commit(", ".rollback(", "async_transaction(", "INSERT ", "UPDATE ", "DELETE "):
        assert banned not in source, f"对账脚本不该出现 {banned}"


def test_the_script_has_no_apply_flag(verify_script: ModuleType) -> None:
    """对账没有 ``--apply``（与清理脚本刻意不同）：一条会对账出分歧的命令，永远不该顺手改点什么。"""

    with pytest.raises(SystemExit) as excinfo:
        verify_script.main(["--apply"])

    assert excinfo.value.code == 2


async def _reconcile(db: AsyncSession, **kwargs: Any) -> Any:
    """跑一遍脚本的对账核心（脚本模块由 `_load_script` 装载，导入即安全）。"""

    script = _load_script()
    return await script.reconcile_document_versions(db, **kwargs)


# ────────────────────────────────────────────── 脚本：打印与退出码（取数打桩）


class _ClosingSession:
    """``_open_session`` 的返回值替身：只关心它被关掉了。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _patch_main(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, report: Any
) -> tuple[_ClosingSession, dict[str, Any]]:
    """把取数换成打桩：这些用例只验**打印与退出码**（取数由上面的真 SQL 用例守）。"""

    session = _ClosingSession()
    captured: dict[str, Any] = {}

    async def _open() -> _ClosingSession:
        return session

    async def _core(_db: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return report

    monkeypatch.setattr(module, "_open_session", _open)
    monkeypatch.setattr(module, "reconcile_document_versions", _core)
    monkeypatch.setattr(module, "setup_logging", lambda: None)
    return session, captured


def test_main_prints_every_check_with_its_count_and_exits_zero(
    verify_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """零分歧：四条逐条打印计数（含 0），退出 0。"""

    report = reconcile_module.reconcile([_facts()], keyword_state=KEYWORD_STATE_CHECKED)
    session, _captured = _patch_main(monkeypatch, verify_script, report)

    exit_code = verify_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "read-only" in out and "all knowledge bases" in out
    assert "documents: 1" in out and "chunks: 3" in out
    for index, label in enumerate(verify_script.CHECK_LABELS.values(), start=1):
        assert f"check {index}/4 {label}" in out
    assert "0 disagreement(s)" in out
    assert "RESULT: OK" in out
    assert session.closed


def test_main_lists_every_disagreement_and_exits_non_zero(
    verify_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**验收：有分歧退出非 0 并逐条列出。**"""

    report = reconcile_module.reconcile(
        [
            _facts(document_id="doc-bad", pointer=2, version_rows=((1, "current", "旧"),)),
            _facts(document_id="doc-ok"),
        ],
        keyword_state=KEYWORD_STATE_CHECKED,
    )
    _patch_main(monkeypatch, verify_script, report)

    exit_code = verify_script.main(["--kb-id", KB])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "1 disagreement(s)" in out
    assert "doc-bad" in out, "分歧要逐条列出（不是只给一个数字）"
    assert "doc-ok" not in out, "没分歧的文档不该出现在明细里"
    assert "kb_id=kb-1" in out
    assert "RESULT: FAILED" in out


def test_main_prints_the_undecidable_keyword_dimension_and_still_exits_zero(
    verify_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**验收：关键词能力不可用时记「不可判定」并明确打印，不判失败。**

    这一条是整套对账里唯一「不能判」的地方；它必须与「数据缺失」分得开，否则运维要么
    去修一个不存在的错，要么把真的分歧当成「没部署 ES」忽略掉。
    """

    report = reconcile_module.reconcile(
        [_facts(keyword_current_versions=None)],
        keyword_state=KEYWORD_STATE_UNDECIDABLE,
        keyword_reason=KEYWORD_REASON_ABSENT,
    )
    _patch_main(monkeypatch, verify_script, report)

    exit_code = verify_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0, "不可判定不是失败"
    assert "undecidable" in out
    assert KEYWORD_REASON_ABSENT in out, "原因要打印出来（能力不存在 ≠ 数据缺失）"
    assert "RESULT: OK" in out
    assert "不可判定" in out


def test_main_reports_the_counters_of_the_two_information_items(
    verify_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """两个信息项**不判失败**，但计数要打印（0 与非 0 正好回答「重建跑完了没有」）。"""

    report = reconcile_module.reconcile(
        [
            _facts(
                pointer=3,
                content="第 3 稿正文。",
                version_rows=((3, "current", "第 3 稿正文。"),),
                chunk_versions=((3, 0),),
                keyword_current_versions=(),
            )
        ],
        keyword_state=KEYWORD_STATE_CHECKED,
    )
    _patch_main(monkeypatch, verify_script, report)

    exit_code = verify_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "pointer drafts without chunks: 1" in out
    assert "keyword drafts without a current-draft slice: 1" in out
    assert "not a failure" in out


def test_main_passes_the_narrowing_flags_down(
    verify_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--kb-id`` / ``--limit`` 原样透传给取数那一层。"""

    report = reconcile_module.reconcile([], keyword_state=KEYWORD_STATE_CHECKED)
    _session, captured = _patch_main(monkeypatch, verify_script, report)

    assert verify_script.main(["--kb-id", KB, "--limit", "5"]) == 0
    assert captured == {"kb_id": KB, "limit": 5}


def test_a_negative_limit_is_rejected_with_exit_code_two(
    verify_script: ModuleType,
) -> None:
    """``--limit -1`` 是非法入参：静默当成 0 会扫零篇却报 OK——最坏的一种假绿。"""

    with pytest.raises(SystemExit) as excinfo:
        verify_script.main(["--limit", "-1"])

    assert excinfo.value.code == 2


def test_the_reconcile_stays_a_run_once_script(verify_script: ModuleType) -> None:
    """不新增调度进程：入口是 ``main(argv)``，Celery 没有 beat 计划（与清理脚本同一条）。"""

    from app.celery_app import celery_app

    assert callable(verify_script.main)
    # 判据从「没有 beat 计划」收紧成「**这个脚本**不在计划里」：第 17 步 #225 给清扫器加了
    # 一条周期任务（``beat_schedule``），而那与「对账 / 清理脚本保持一次性」并不冲突——
    # 用「计划为空」当证据会把两件事绑在一起，于是一次正当的调度新增就会把这条护栏撞红，
    # 而它想守的东西（**脚本本身**不许被排进调度）反而没被验到。
    schedule = getattr(celery_app.conf, "beat_schedule", None) or {}
    scheduled_tasks = {entry.get("task") for entry in schedule.values()} if schedule else set()
    assert verify_script.__name__ not in scheduled_tasks
    assert not any(
        "reconcile" in str(name) or "retention" in str(name) for name in scheduled_tasks
    ), "对账 / 清理脚本不进调度基础设施"
