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
from app.repositories import chunk_repository, document_repository, document_version_repository
from app.services import document_version_reconcile as reconcile_module
from app.services.document_version_reconcile import (
    KEYWORD_REASON_ABSENT,
    KEYWORD_REASON_FAILED,
    KEYWORD_STATE_CHECKED,
    KEYWORD_STATE_UNDECIDABLE,
    DocumentFacts,
)

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _facts(
    *,
    document_id: str = "doc-1",
    pointer: int = 2,
    content: str = "当前稿正文。",
    version_rows: Sequence[tuple[int, str, str]] = ((1, "archived", "旧稿正文。"), (2, "current", "当前稿正文。")),
    chunk_versions: Sequence[tuple[int, int]] = ((2, 3),),
    keyword_current_versions: Sequence[int] | None = (2,),
) -> DocumentFacts:
    """一篇文档的对账读数。

    `chunk_versions` 是 ``(稿号, 这一稿有几片)`` 的列表（仓储就是按这对值分组的），
    `keyword_current_versions` 是关键词索引里标着「当前稿」的稿号集合（``None`` = 不可判定）。
    """

    return DocumentFacts(
        document_id=document_id,
        pointer=pointer,
        content=content,
        version_rows=tuple(version_rows),
        chunk_versions=tuple(chunk_versions),
        keyword_current_versions=(
            None if keyword_current_versions is None else tuple(keyword_current_versions)
        ),
    )


def _report(*facts: DocumentFacts, keyword_state: str = KEYWORD_STATE_CHECKED, reason=None):
    return reconcile_module.reconcile(
        facts, keyword_state=keyword_state, keyword_reason=reason
    )


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

    report = _report(
        _facts(document_id="doc-9", pointer=2, version_rows=((1, "current", "旧"),))
    )

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
    doc_version INTEGER NOT NULL DEFAULT '1'
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


#: 三篇文档：`doc-1` 两稿（指针 2）、`doc-2` 一稿、`doc-3` 在另一个库里。
SEEDED_DOCUMENTS = (
    _document("doc-1", current_version=2, content="第 2 稿正文。"),
    _document("doc-2", content="第 1 稿正文。"),
    _document("doc-3", kb_id=OTHER_KB, content="别的库。"),
)

SEEDED_VERSIONS = (
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
        for row in (*SEEDED_DOCUMENTS, *SEEDED_VERSIONS):
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

    rows = await document_version_repository.list_for_documents(
        reconcile_db, ["doc-1", "doc-2"]
    )

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

