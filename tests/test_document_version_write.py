"""#175 · 建版本服务：写路径的顺序、守卫、判重与冲突映射（真 SQL，真唯一约束）。

这一组不经过 HTTP：``document_version_service.create_version`` 是那条写路径的全部逻辑，
请求边界（``async with async_transaction(db)``）在用例里显式圈出来，于是「提交了一次」
「出错整笔回滚」「副作用登记在提交之后」都能被直接看见。

用 ``sqlite+aiosqlite`` 而不是替身会话：这里要验的是**唯一约束真的会把重复稿号打回来**
（冲突映射成 20029 的前提），以及 ``UPDATE`` 的范围条件真按文档收窄——替身会话对这两件
事只会点头。真 PostgreSQL 的行锁语义不在这一层验（见
``tests/test_document_version_repository.py`` 的模块文档）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import async_transaction
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.plan import PLAN_PRESETS, PLAN_PRO
from app.repositories import document_version_repository
from app.services import document_version_service
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
DOC_ID = "doc-1"
OLD_CONTENT = "第 1 稿正文：退款流程见附件。"
NEW_CONTENT = "第 2 稿正文：退款流程见附件，另需工单号。"


@pytest_asyncio.fixture
async def env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    """一份已索引成功的文档（第 1 稿），加两张真表与一个临时存储根。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(DocumentVersion.__table__.create)
    monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", str(tmp_path / "uploads"))
    async with AsyncSession(engine) as session:
        session.add(
            Document(
                id=DOC_ID,
                tenant_id=TENANT_ID,
                kb_id="kb-1",
                title="积分规则",
                content=OLD_CONTENT,
                status=DocumentStatus.SUCCESS,
                current_version=1,
            )
        )
        session.add(
            DocumentVersion(
                id="v1",
                document_id=DOC_ID,
                version=1,
                content=OLD_CONTENT,
                checksum=_checksum(OLD_CONTENT),
                status="current",
                created_at=datetime(2026, 11, 15, 0, 0, 0),
            )
        )
        await session.commit()
        try:
            yield SimpleNamespace(db=session, engine=engine, storage=tmp_path / "uploads")
        finally:
            await session.rollback()
    await engine.dispose()


def _checksum(text: str) -> str:
    """期望值来自**唯一入口**（``app.utils.checksum``），不是这里另算一遍。

    这条不算同义反复：它钉的是「写进列的 checksum 与判重用的口径是同一份」。
    """

    from app.utils.checksum import checksum_of

    return checksum_of(text)


async def _rows(db: AsyncSession) -> list[DocumentVersion]:
    return list(
        (
            await db.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == DOC_ID)
                .order_by(DocumentVersion.version)
            )
        )
        .scalars()
        .all()
    )


async def _submit(
    env: SimpleNamespace,
    *,
    content: str | None = NEW_CONTENT,
    filename: str | None = None,
    data: bytes = b"",
    tenant_id: str = TENANT_ID,
) -> dict:
    """走**请求边界**的那种写法：一笔事务，成功提交、异常回滚。"""

    async with async_transaction(env.db):
        return await document_version_service.create_version(
            env.db,
            tenant_id=tenant_id,
            document_id=DOC_ID,
            content=content,
            filename=filename,
            data=data,
        )


# ── 提交新内容 ──


@pytest.mark.asyncio
async def test_new_content_creates_the_next_draft_and_archives_the_old_one(
    env: SimpleNamespace,
) -> None:
    """新稿次 + 旧稿归档 + 指针前移 + 文档置处理中，四件事在同一笔里。"""

    result = await _submit(env)

    assert result["version"] == 2
    assert result["previous_version"] == 1
    assert result["document_id"] == DOC_ID

    rows = await _rows(env.db)
    assert [(row.version, row.status) for row in rows] == [(1, "archived"), (2, "current")]
    assert rows[1].content == NEW_CONTENT
    assert rows[1].checksum == _checksum(NEW_CONTENT)

    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 2, "当前稿指针必须前移"
    assert doc.content == NEW_CONTENT, "文档行是当前稿的热路径快照"
    assert doc.status == DocumentStatus.QUEUED, "新稿要重建，所以置排队中（等 Worker 接走）"
    assert doc.index_claimed_at is None, "排队不盖租约：Worker 才是唯一抢占者"


@pytest.mark.asyncio
async def test_the_version_row_stores_the_normalized_text(env: SimpleNamespace) -> None:
    """行里存的是规范化之后的正文：``checksum_of(row.content) == row.checksum`` 恒成立。

    不规范化就把原始写法存进去（``\\r\\n``、首尾空格），那条等式只在写入那一刻成立，
    之后任何一次「重新算一遍校验和」的对账都会把这行判成不一致。
    """

    await _submit(env, content="  第 2 稿\r\n正文：含 Windows 换行与首尾空格。  ")

    rows = await _rows(env.db)
    assert rows[1].content == "第 2 稿\n正文：含 Windows 换行与首尾空格。"
    assert rows[1].checksum == _checksum(rows[1].content)


# ── 内容未变 ──


@pytest.mark.asyncio
async def test_resubmitting_the_current_draft_writes_nothing(env: SimpleNamespace) -> None:
    """与当前稿相同 → 「内容未变」（20031）；版本行数不变，文档行一个字节都不动。

    判重比的是**规范化后的校验和**，不是原文：换行风格不同但内容相同也算没变。
    """

    with pytest.raises(AppError) as caught:
        await _submit(env, content="  第 1 稿正文：退款流程见附件。\r\n")

    assert caught.value.code is ErrorCode.DOCUMENT_VERSION_UNCHANGED
    assert caught.value.data == {"version": 1}

    assert [row.version for row in await _rows(env.db)] == [1]
    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 1
    assert doc.status == DocumentStatus.SUCCESS, "什么都没写：状态也不该被改成处理中"


# ── 与历史某稿相同 ──


@pytest.mark.asyncio
async def test_resubmitting_an_archived_draft_creates_a_new_draft(
    env: SimpleNamespace,
) -> None:
    """与**历史**某稿相同 → 建新稿次，**不复活**旧行。

    时间轴单调（D 系列用户故事第 10 条）：回到旧内容是一次新的变更，中间那几次不抹掉。
    """

    env.db.add(
        DocumentVersion(
            id="v2",
            document_id=DOC_ID,
            version=2,
            content=NEW_CONTENT,
            checksum=_checksum(NEW_CONTENT),
            status="current",
            created_at=datetime(2026, 11, 15, 1, 0, 0),
        )
    )
    doc = await env.db.get(Document, DOC_ID)
    doc.current_version = 2
    doc.content = NEW_CONTENT
    await env.db.flush()
    await document_version_repository.archive_current_except(
        env.db,
        DOC_ID,
        keep=2,
    )
    # 让第 1 稿回到「归档」状态，检查第三次提交会不会顺手把那一行改回当前稿。
    await env.db.commit()

    result = await _submit(env, content=OLD_CONTENT)

    assert result["version"] == 3
    rows = await _rows(env.db)
    assert [(row.version, row.status) for row in rows] == [
        (1, "archived"),
        (2, "archived"),
        (3, "current"),
    ]
    assert rows[2].content == OLD_CONTENT


# ── 冲突 ──


@pytest.mark.asyncio
async def test_a_duplicate_draft_number_maps_to_conflict_instead_of_500(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发撞唯一约束 → 20029「请重试」，而不是 500。

    **怎么造出来的**：真 PostgreSQL 上两个提交会由文档行锁串行化，撞不上唯一约束；
    能撞上的是「读 max 读到的是旧快照、另一个人已经写进去了」这类窗口。这里用
    ``max_version`` 的替身把那个窗口钉死：库里已经有第 2 稿（竞争者的），而这次写入
    算出来的下一个号仍然是 2 —— 于是 ``flush`` 撞的是**真实**的
    ``(document_id, version)`` 唯一约束，不是模拟出来的异常。
    """

    env.db.add(
        DocumentVersion(
            id="v2",
            document_id=DOC_ID,
            version=2,
            content="竞争者的第 2 稿正文。",
            checksum=_checksum("竞争者的第 2 稿正文。"),
            status="current",
            created_at=datetime(2026, 11, 15, 1, 0, 0),
        )
    )
    seeded_doc = await env.db.get(Document, DOC_ID)
    seeded_doc.current_version = 2
    seeded_doc.content = "竞争者的第 2 稿正文。"
    await env.db.flush()
    await document_version_repository.archive_current_except(env.db, DOC_ID, keep=2)
    await env.db.commit()

    monkeypatch.setattr(document_version_repository, "max_version", _stale_max)

    with pytest.raises(AppError) as caught:
        await _submit(env)

    assert caught.value.code is ErrorCode.DOCUMENT_VERSION_CONFLICT
    assert "重试" in caught.value.msg

    # 整笔回滚：竞争者的第 2 稿还在，没有第三条被写进来，指针也没有被挪。
    rows = await _rows(env.db)
    assert [(row.version, row.status) for row in rows] == [(1, "archived"), (2, "current")]
    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 2, "冲突的那一笔不该留下「指针已经切过去」的半份状态"


async def _stale_max(*_args: Any, **_kwargs: Any) -> int:
    """「这次读 max 读到的是旧快照」（竞争者的第 2 稿在它之后才落库）。"""

    return 1


# ── 守卫 ──


@pytest.mark.asyncio
async def test_a_processing_document_refuses_new_versions(env: SimpleNamespace) -> None:
    """有效处理中（租约新鲜）→ 20025，复用既有的「文档处理中」，不新造码。"""

    doc = await env.db.get(Document, DOC_ID)
    from app.db.base import now

    doc.status = DocumentStatus.PROCESSING
    doc.index_claimed_at = now()
    await env.db.commit()

    with pytest.raises(AppError) as caught:
        await _submit(env)

    assert caught.value.code is ErrorCode.DOCUMENT_IS_PROCESSING
    assert [row.version for row in await _rows(env.db)] == [1]


@pytest.mark.asyncio
async def test_an_unknown_or_cross_tenant_document_is_not_found(env: SimpleNamespace) -> None:
    """跨租户一律「不存在」（20016）：不返回「无权限」，不泄露资源存在性。"""

    with pytest.raises(AppError) as caught:
        await _submit(env, tenant_id="tenant-b")

    assert caught.value.code is ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_empty_content_is_rejected_before_anything_is_written(
    env: SimpleNamespace,
) -> None:
    """空正文（含只有空白的）按既有的 20024 拒：没有正文的稿次没有意义。"""

    with pytest.raises(AppError) as caught:
        await _submit(env, content="   \r\n ")

    assert caught.value.code is ErrorCode.DOCUMENT_CONTENT_EMPTY
    assert [row.version for row in await _rows(env.db)] == [1]


# ── 文件模式 ──


@pytest.mark.asyncio
async def test_file_mode_parses_the_upload_and_keeps_its_own_source_file(
    env: SimpleNamespace,
) -> None:
    """文件模式走既有解析入口；源文件按**稿号**落盘，绝不覆盖原始上传的那一份。

    两份文件抄同一个路径（``<doc_id>.md``）的代价是：重解析第 1 稿时读到的是第 2 稿的
    字节，而症状是「重建之后内容莫名其妙变了」——所以路径里必须带稿号。
    """

    result = await _submit(
        env,
        content=None,
        filename="notes.md",
        data="# 第 2 稿\n\n退款需工单号。".encode(),
    )

    assert result["version"] == 2
    rows = await _rows(env.db)
    assert rows[1].content == "# 第 2 稿\n\n退款需工单号。"
    assert rows[1].source_file_path is not None
    assert ".v2." in rows[1].source_file_path
    assert (env.storage / rows[1].source_file_path).exists()

    doc = await env.db.get(Document, DOC_ID)
    assert doc.source_file_path == rows[1].source_file_path, "文档行随当前稿的源文件走"
    assert doc.source_filename == "notes.md"
