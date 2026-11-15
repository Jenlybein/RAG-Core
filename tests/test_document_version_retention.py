"""#178 · 保留窗口：留最近 3 稿，窗口外的**归档**稿连切片带版本行一起清掉。

三层接缝，都是既有的：

* **仓储层**（``sqlite+aiosqlite`` 的真 SQL）：窗口查询只挑「这篇文档、稿号在某条界
  以下、状态是归档」的行；删除只按给定稿号打，且**不提交**（提交点只有事务入口那一处）；
* **服务层**（真 SQL + 一个记账用的切片存储替身）：五稿造出来 → 清理后剩 3/4/5；
  切片的删除键是两个维度（``doc_version`` 给了、``index_version`` 留空）；先删切片再删
  版本行；中途失败整笔回滚，不留「切片已删、版本行还在」的半份；
* **运维脚本**（``main(argv) -> int``）：默认 dry-run 只打印将删什么、``--apply`` 才动手、
  ``--retain`` 覆盖默认 3、退出码沿用 ``_ops_common.exit_code_for``。

脚本通过文件路径加载（``scripts*`` 不在安装包里），顺带验证「导入即安全」：模块顶层不
解析 argv、不连库、不建引擎。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.transaction import async_transaction
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.repositories import document_repository, document_version_repository
from app.services import document_version_service
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
KB_ID = "kb-1"
OTHER_KB_ID = "kb-2"
DOC_ID = "doc-1"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


# ─────────────────────────────────────────────────── 夹具：两张真表（aiosqlite）


@pytest_asyncio.fixture
async def retention_db() -> AsyncIterator[AsyncSession]:
    """``documents`` + ``document_versions`` 两张真表。

    用真 SQL 而不是替身会话：这一票要验的正是「``WHERE`` 的范围条件把哪几行筛出来」
    「``DELETE ... IN (...)`` 真的删掉了几行」——替身会话对这些只会点头。
    """

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(DocumentVersion.__table__.create)
    # ``expire_on_commit=False`` 与生产会话工厂一致（``app.db.async_session.AsyncSessionLocal``）：
    # 清理脚本对每篇文档各开一笔事务，提交之后仍要读这一行的指针 / 租户，用默认的
    # ``expire_on_commit=True`` 会在套件里造出一条生产上不存在的失败路径（``MissingGreenlet``）。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()


def _document(
    *,
    document_id: str,
    current_version: int = 1,
    kb_id: str = KB_ID,
    tenant_id: str = TENANT_ID,
) -> Document:
    return Document(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        title=document_id,
        content=f"{document_id} 的正文",
        status=DocumentStatus.SUCCESS,
        current_version=current_version,
    )


def _version(
    *,
    document_id: str,
    version: int,
    status: str = "archived",
) -> DocumentVersion:
    return DocumentVersion(
        id=f"{document_id}-v{version}",
        document_id=document_id,
        version=version,
        content=f"{document_id} 第 {version} 稿正文",
        checksum="0" * 64,
        status=status,
        created_at=datetime(2026, 11, 16, 0, 0, 0),
    )


async def _seed(db: AsyncSession, *rows: object) -> None:
    for row in rows:
        db.add(row)
    await db.flush()


async def _versions_of(db: AsyncSession, document_id: str) -> list[tuple[int, str]]:
    """这篇文档剩下的 (稿号, 状态)，按稿号升序。"""

    rows = (
        await db.execute(
            select(DocumentVersion.version, DocumentVersion.status)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


# ──────────────────────────────────────────── 仓储：窗口查询（只挑归档稿）


@pytest.mark.asyncio
async def test_window_query_returns_archived_versions_at_or_below_the_bound(
    retention_db: AsyncSession,
) -> None:
    """``version <= 界`` 的**归档**行：当前稿行与更高稿号一律不动。

    「归档」这个条件是「当前稿永不删」的落点——它不是服务里的一句 if，而是查询的
    ``WHERE``：上面那行 ``current`` 就算稿号落在窗口外（库里残留一条低稿号的当前行），
    这条查询也永远不会把它交出去。
    """

    await _seed(
        retention_db,
        _version(document_id="doc-a", version=1, status="archived"),
        _version(document_id="doc-a", version=2, status="archived"),
        _version(document_id="doc-a", version=3, status="archived"),
        _version(document_id="doc-a", version=4, status="current"),
        _version(document_id="doc-b", version=1, status="archived"),
    )

    rows = await document_version_repository.list_archived_at_or_below(
        retention_db, "doc-a", version=2
    )

    assert [row.version for row in rows] == [2, 1], "只挑该文档、该界以下的归档稿"


@pytest.mark.asyncio
async def test_window_query_keeps_a_low_current_pointer_row_even_below_the_bound(
    retention_db: AsyncSession,
) -> None:
    """指针在 v9、当前稿行却还是 v1（历史事故）时，v1 依然不许被扫出来。

    窗口按**指针**算，但「删谁」按**状态**判：两条口径分开写，才会在数据不正常时仍然
    保住「当前稿永不删」。
    """

    await _seed(
        retention_db,
        _document(document_id="doc-a", current_version=9),
        _version(document_id="doc-a", version=1, status="current"),
        _version(document_id="doc-a", version=8, status="archived"),
    )

    rows = await document_version_repository.list_archived_at_or_below(
        retention_db, "doc-a", version=6
    )

    assert [row.version for row in rows] == []


# ──────────────────────────────────────────── 仓储：按稿号删（不提交）


@pytest.mark.asyncio
async def test_deleting_versions_touches_only_the_given_drafts_of_that_document(
    retention_db: AsyncSession,
) -> None:
    """``DELETE ... IN (稿号)`` 只打这篇文档的这几个稿号，别的文档原样留着。"""

    await _seed(
        retention_db,
        _version(document_id="doc-a", version=1),
        _version(document_id="doc-a", version=2),
        _version(document_id="doc-a", version=3),
        _version(document_id="doc-b", version=1),
    )
    await retention_db.commit()

    await document_version_repository.delete_versions(retention_db, "doc-a", (1, 2))

    assert await _versions_of(retention_db, "doc-a") == [(3, "archived")]
    assert await _versions_of(retention_db, "doc-b") == [(1, "archived")]


@pytest.mark.asyncio
async def test_deleting_versions_does_not_commit_and_an_empty_list_is_a_no_op(
    retention_db: AsyncSession,
) -> None:
    """仓储不提交（提交点只有事务入口）；没有要删的稿号时不发语句、不报错。"""

    await _seed(retention_db, _version(document_id="doc-a", version=1))
    await retention_db.commit()

    await document_version_repository.delete_versions(retention_db, "doc-a", ())
    assert await _versions_of(retention_db, "doc-a") == [(1, "archived")]

    await document_version_repository.delete_versions(retention_db, "doc-a", (1,))
    assert await _versions_of(retention_db, "doc-a") == [], "DELETE 已经发出去了"

    await retention_db.rollback()

    assert await _versions_of(retention_db, "doc-a") == [(1, "archived")], (
        "仓储自己提交过就会在这里露出来"
    )


# ──────────────────────────────────────── 文档扫描：窗口可能非空的那些文档


@pytest.mark.asyncio
async def test_scan_returns_only_documents_whose_window_can_be_non_empty(
    retention_db: AsyncSession,
) -> None:
    """``current_version > retain`` 才可能有过期稿（``current - retain >= 1``）。

    边界写成 ``>`` 不是 ``>=``：稿号 3 而窗口留 3 稿时，窗口下界是 0，
    一篇文档都不该被扫出来——``>=`` 会让运行到「第 4 稿」之前的每个文档都白跑一趟。
    """

    await _seed(
        retention_db,
        _document(document_id="doc-3", current_version=3),
        _document(document_id="doc-4", current_version=4),
        _document(document_id="doc-5", current_version=5),
        _document(document_id="doc-6-other-kb", current_version=6, kb_id=OTHER_KB_ID),
    )

    found = await document_repository.list_with_expired_versions(retention_db, retain=3)

    assert {doc.id for doc in found} == {"doc-4", "doc-5", "doc-6-other-kb"}


@pytest.mark.asyncio
async def test_scan_can_be_narrowed_by_kb_and_capped(retention_db: AsyncSession) -> None:
    """``--kb-id`` 收窄范围、``--limit`` 收敛条数：运维先看一小批再决定。"""

    await _seed(
        retention_db,
        _document(document_id="doc-a", current_version=9),
        _document(document_id="doc-b", current_version=9),
        _document(document_id="doc-c", current_version=9, kb_id=OTHER_KB_ID),
    )

    only_kb = await document_repository.list_with_expired_versions(
        retention_db, retain=3, kb_id=KB_ID
    )
    capped = await document_repository.list_with_expired_versions(
        retention_db, retain=3, kb_id=KB_ID, limit=1
    )

    assert {doc.id for doc in only_kb} == {"doc-a", "doc-b"}
    assert len(capped) == 1


# ───────────────────────────────── 服务层：窗口按稿数算（纯函数，先钉口径）


@pytest.mark.parametrize(
    ("current_version", "retain", "expected_floor"),
    [
        (5, 3, 2),
        (4, 3, 1),
        (3, 3, 0),
        (9, 1, 8),
        (1, 3, -2),
    ],
)
def test_the_window_floor_is_the_current_draft_minus_the_retain_count(
    current_version: int, retain: int, expected_floor: int
) -> None:
    """``当前稿号 - 保留稿数``：按**稿数**算，与时间无关（D10 / 用户故事 29）。

    期望值是手算的字面量，不是「用同一个减法再算一遍」：这条钉的是「窗口就在稿号轴上」，
    任何按 ``created_at`` 收窄的改法都会在这里露出来。
    """

    assert (
        document_version_service.retention_floor(current_version, retain)
        == expected_floor
    )


def test_the_default_window_retains_three_drafts() -> None:
    """默认留 3 稿是**业务参数**（常量），不是环境变量 / 配置项。"""

    assert document_version_service.RETAIN_VERSIONS == 3


def test_the_plan_keeps_the_drafts_inside_the_window_and_expires_the_rest() -> None:
    """五稿（1–4 归档、5 当前）留 3 稿 → 该清 2 和 1，且最老的排在前面。

    顺序不是装饰：清理按这个顺序逐稿去清切片，先走最老的，失败时日志里的「清到哪一稿」
    才对得上运维的直觉。
    """

    plan = document_version_service.retention_plan(
        document_id=DOC_ID,
        current_version=5,
        archived_versions=[4, 3, 2, 1],
        retain=3,
    )

    assert plan.expired_versions == (2, 1)
    assert plan.floor == 2
    assert not plan.is_empty


def test_the_plan_is_empty_when_every_archived_draft_is_inside_the_window() -> None:
    """窗口内的归档稿一个都不许清：只长到第 3 稿时，一篇也没有过期稿。"""

    plan = document_version_service.retention_plan(
        document_id=DOC_ID, current_version=3, archived_versions=[2, 1], retain=3
    )

    assert plan.expired_versions == ()
    assert plan.is_empty


# ─────────────────────────────────────────── 服务层：真的清（真 SQL + 存储替身）


class _RecordingStore:
    """切片存储替身：记下每次删除的**删除键**，并能在删的当场看一眼版本行还在不在。

    ``rows_at_call`` 是「先删切片、再删版本行」这条顺序的直接证据：真到删切片那一刻，
    版本行必须**全都还在**。``fail_on_version`` 让某一次删除抛错，用来验整笔回滚。
    """

    def __init__(self, *, fail_on_version: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows_at_call: list[list[tuple[int, str]]] = []
        self._fail_on_version = fail_on_version

    async def delete_by_document_id(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
        doc_version: int | None = None,
    ) -> None:
        self.calls.append(
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "index_version": index_version,
                "doc_version": doc_version,
            }
        )
        self.rows_at_call.append(await _versions_of(db, document_id))
        if self._fail_on_version is not None and doc_version == self._fail_on_version:
            raise RuntimeError("keyword store is down")


async def _seed_five_drafts(db: AsyncSession) -> None:
    """五稿：1–4 归档、5 当前，指针在 5（窗口留 3 稿时的典型形态）。"""

    await _seed(
        db,
        _document(document_id=DOC_ID, current_version=5),
        *[
            _version(
                document_id=DOC_ID,
                version=version,
                status="current" if version == 5 else "archived",
            )
            for version in range(1, 6)
        ],
    )
    await db.commit()


@pytest.mark.asyncio
async def test_five_drafts_shrink_to_the_last_three(retention_db: AsyncSession) -> None:
    """**验收：造 5 稿 → 清理后剩 3、4、5。**"""

    await _seed_five_drafts(retention_db)
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    vector, keyword = _RecordingStore(), _RecordingStore()
    async with async_transaction(retention_db):
        plan = await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[vector, keyword]
        )

    assert plan.expired_versions == (2, 1)
    assert await _versions_of(retention_db, DOC_ID) == [
        (3, "archived"),
        (4, "archived"),
        (5, "current"),
    ]
    assert [call["doc_version"] for call in vector.calls] == [2, 1], (
        "向量存储里这一稿的切片要清掉"
    )
    assert [call["doc_version"] for call in keyword.calls] == [2, 1], (
        "关键词存储是第二个存储，同样要清"
    )


@pytest.mark.asyncio
async def test_chunk_deletion_key_carries_the_draft_and_leaves_index_version_open(
    retention_db: AsyncSession,
) -> None:
    """删切片的键是**两维**的：``doc_version=这一稿``、``index_version=None``。

    这一稿的切片可能散落在**多个索引版本**里（每次重建写一套），只删当前索引版本会留下
    旧索引版本下的幽灵切片——它们永远不会被任何查询命中（检索 pin 在生效版本上），
    却会一直占着向量库与关键词索引。所以清理必须按稿号清、版本放开。
    """

    await _seed_five_drafts(retention_db)
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    store = _RecordingStore()
    async with async_transaction(retention_db):
        await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[store]
        )

    assert [call["index_version"] for call in store.calls] == [None, None]
    assert [call["doc_version"] for call in store.calls] == [2, 1]
    assert {call["tenant_id"] for call in store.calls} == {TENANT_ID}, (
        "清除也强制带租户（ADR-0006）"
    )
    assert {call["document_id"] for call in store.calls} == {DOC_ID}


@pytest.mark.asyncio
async def test_chunks_are_deleted_before_the_version_rows(retention_db: AsyncSession) -> None:
    """顺序是契约：清切片那一刻，这一篇的五条版本行**全都还在**。

    反过来（先删版本行）时，事务一旦回滚就留下「行没了、切片还在」的孤儿——那批切片
    永远查不到、也不知道属于哪一稿。
    """

    await _seed_five_drafts(retention_db)
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    store = _RecordingStore()
    async with async_transaction(retention_db):
        await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[store]
        )

    assert [len(rows) for rows in store.rows_at_call] == [5, 5], (
        "清切片时版本行必须还没删"
    )


@pytest.mark.asyncio
async def test_the_current_draft_is_never_deleted_even_when_it_falls_outside_the_window(
    retention_db: AsyncSession,
) -> None:
    """指针在 9、当前稿行却还是 1（历史事故）：v1 落在窗口外也不许删。

    「只删归档稿」是**状态**条件，「窗口」是**稿号**条件；两者分开写，数据不正常时
    仍然是当前稿优先。
    """

    await _seed(
        retention_db,
        _document(document_id=DOC_ID, current_version=9),
        _version(document_id=DOC_ID, version=1, status="current"),
        *[
            _version(document_id=DOC_ID, version=version, status="archived")
            for version in range(2, 9)
        ],
    )
    await retention_db.commit()
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    store = _RecordingStore()
    async with async_transaction(retention_db):
        plan = await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[store]
        )

    assert plan.expired_versions == (6, 5, 4, 3, 2)
    remaining = await _versions_of(retention_db, DOC_ID)
    assert (1, "current") in remaining, "当前稿永远在"
    assert [version for version, _status in remaining] == [1, 7, 8]


@pytest.mark.asyncio
async def test_a_failure_while_purging_rolls_the_whole_document_back(
    retention_db: AsyncSession,
) -> None:
    """中途失败整笔回滚：不留「切片已删、版本行还在」或「版本行删了一半」的半份。"""

    await _seed_five_drafts(retention_db)
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    vector = _RecordingStore()
    keyword = _RecordingStore(fail_on_version=1)

    with pytest.raises(RuntimeError, match="keyword store is down"):
        async with async_transaction(retention_db):
            await document_version_service.purge_expired_versions(
                retention_db, document=document, retain=3, stores=[vector, keyword]
            )

    assert [len(rows) for rows in vector.rows_at_call] == [5, 5], "两稿都先清了切片"
    assert await _versions_of(retention_db, DOC_ID) == [
        (1, "archived"),
        (2, "archived"),
        (3, "archived"),
        (4, "archived"),
        (5, "current"),
    ], "事务回滚：版本行一条都不能少"


@pytest.mark.asyncio
async def test_purging_twice_is_a_no_op_the_second_time(retention_db: AsyncSession) -> None:
    """幂等：清理完再跑一遍，没有候选、不发 DELETE、不碰存储。"""

    await _seed_five_drafts(retention_db)
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    async with async_transaction(retention_db):
        await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[_RecordingStore()]
        )

    store = _RecordingStore()
    async with async_transaction(retention_db):
        plan = await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[store]
        )

    assert plan.is_empty
    assert store.calls == []
    assert await _versions_of(retention_db, DOC_ID) == [
        (3, "archived"),
        (4, "archived"),
        (5, "current"),
    ]


@pytest.mark.asyncio
async def test_a_smaller_retain_count_widens_the_window(retention_db: AsyncSession) -> None:
    """``retain`` 覆盖默认 3：留 2 稿时第 3 稿也出局（脚本的 ``--retain`` 走的就是它）。"""

    await _seed_five_drafts(retention_db)
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    store = _RecordingStore()
    async with async_transaction(retention_db):
        plan = await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=2, stores=[store]
        )

    assert plan.expired_versions == (3, 2, 1)
    assert await _versions_of(retention_db, DOC_ID) == [(4, "archived"), (5, "current")]


@pytest.mark.asyncio
async def test_the_window_counts_drafts_not_time(retention_db: AsyncSession) -> None:
    """窗口按稿数算：十年前建的稿子只要在窗口内就留着，一分钟前建的过期稿照样清。

    时间是**无关变量**——所以这里把日期摆成「越老越新都在窗口里」，任何按
    ``created_at`` 收窄的改法都会把老稿子清掉，然后在这条用例上红。
    """

    await _seed(
        retention_db,
        _document(document_id=DOC_ID, current_version=5),
        *[
            DocumentVersion(
                id=f"{DOC_ID}-v{version}",
                document_id=DOC_ID,
                version=version,
                content=f"第 {version} 稿",
                checksum="0" * 64,
                status="current" if version == 5 else "archived",
                # 窗口内的第 3、4、5 稿是最早创建的三行（2015 年），窗口外的第 1、2 稿
                # 反而是最新的——按时间清会把该留的清掉。
                created_at=datetime(2015, 1, 1) if version >= 3 else datetime(2026, 11, 16),
            )
            for version in range(1, 6)
        ],
    )
    await retention_db.commit()
    document = await document_repository.get_by_id(retention_db, DOC_ID)
    assert document is not None

    async with async_transaction(retention_db):
        plan = await document_version_service.purge_expired_versions(
            retention_db, document=document, retain=3, stores=[_RecordingStore()]
        )

    assert plan.expired_versions == (2, 1)
    assert await _versions_of(retention_db, DOC_ID) == [
        (3, "archived"),
        (4, "archived"),
        (5, "current"),
    ]
