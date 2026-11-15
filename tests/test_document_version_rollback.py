"""#176 · 回滚服务：切指针 = 一笔事务里的五件事，副作用登记在提交之后。

这一组和 ``test_document_version_write.py`` 同层（服务 + 真 SQLite 表），区别只在
「时间轴往后长一稿」换成「指针拨回旧稿」：两稿的身份调换过来了——**目标稿转 current、
当前稿转 archived**，而写路径的其余部分（守卫、锁、文档行同步、``after_commit`` 里的
两件副作用）逐条相同。

用真 SQLite 而不是替身会话的原因也一样：这里要验的是 ``UPDATE ... WHERE`` 真按文档
与稿号收窄（替身只会点头），以及「整笔要么全成、要么全不动」这条红线。

验收项与用例的对应关系（票面顺序）：

* 回滚 = 切指针 + 归档当前稿 + 置处理中 + 提交后投递重建 + 翻转关键词索引；
* 回滚到当前稿 → 「已是当前稿」（20032），什么都不写；
* 文档处理中 → 拒绝（20025，复用既有码）；
* 目标稿已被保留策略物理删除 → 「稿不存在」（20030，不做墓碑）；
* 跨租户一律「不存在」（20016），不泄露存在性。
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

from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import async_transaction
from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.plan import PLAN_PRESETS, PLAN_PRO
from app.services import document_version_service, keyword_draft
from app.utils.status import DocumentStatus

TENANT_ID = "tenant-a"
DOC_ID = "doc-1"

CONTENTS = {
    1: "第 1 稿正文：退款流程见附件。",
    2: "第 2 稿正文：退款流程见附件，另需工单号。",
    3: "第 3 稿正文：退款流程改为线上申请。",
}


def _checksum(text: str) -> str:
    from app.utils.checksum import checksum_of

    return checksum_of(text)


@pytest_asyncio.fixture
async def env(tmp_path: Any) -> AsyncIterator[SimpleNamespace]:
    """一份已经改到第 3 稿的文档：三行版本行，指针在 3，状态已成功。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(DocumentVersion.__table__.create)
    async with AsyncSession(engine) as session:
        session.add(
            Document(
                id=DOC_ID,
                tenant_id=TENANT_ID,
                kb_id="kb-1",
                title="积分规则",
                content=CONTENTS[3],
                status=DocumentStatus.SUCCESS,
                current_version=3,
                # 文档行的源文件随**当前稿**的源文件走（#175）：回滚之后它必须跟着换，
                # 否则重解析会读到第 3 稿的字节。
                source_file_path="tenant-a/doc-1.v3.md",
                source_filename="v3.md",
            )
        )
        for number in (1, 2, 3):
            session.add(
                DocumentVersion(
                    id=f"v{number}",
                    document_id=DOC_ID,
                    version=number,
                    content=CONTENTS[number],
                    checksum=_checksum(CONTENTS[number]),
                    source_file_path=None
                    if number == 2
                    else f"tenant-a/doc-1.v{number}.md",
                    status="current" if number == 3 else "archived",
                    created_at=datetime(2026, 11, 15, number, 0, 0),
                )
            )
        await session.commit()
        try:
            yield SimpleNamespace(db=session, engine=engine, storage=tmp_path / "uploads")
        finally:
            await session.rollback()
    await engine.dispose()


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


async def _rollback(
    env: SimpleNamespace,
    version: int = 1,
    *,
    tenant_id: str = TENANT_ID,
) -> dict:
    """走**请求边界**的写法：一笔事务，成功提交、异常回滚。"""

    async with async_transaction(env.db):
        return await document_version_service.rollback_to_version(
            env.db,
            tenant_id=tenant_id,
            document_id=DOC_ID,
            version=version,
            plan=PLAN_PRESETS[PLAN_PRO],
        )


# ── 五件事：切指针、归档当前稿、置处理中、同步正文、投递 + 翻转在提交之后 ──


@pytest.mark.asyncio
async def test_rollback_moves_the_pointer_back_and_reopens_the_document(
    env: SimpleNamespace,
) -> None:
    """回滚到第 1 稿：两稿身份调换、指针回到 1、文档正文与源文件随目标稿、状态置处理中。"""

    result = await _rollback(env, 1)

    assert result["document_id"] == DOC_ID
    assert result["version"] == 1
    assert result["previous_version"] == 3

    rows = await _rows(env.db)
    assert [(row.version, row.status) for row in rows] == [
        (1, "current"),
        (2, "archived"),
        (3, "archived"),
    ]

    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 1, "当前稿指针必须回到 1"
    assert doc.content == CONTENTS[1], "文档行是当前稿的热路径快照"
    assert doc.status == DocumentStatus.PROCESSING, "旧稿的切片要重建，所以置处理中"
    assert doc.index_claimed_at is None, "排队不盖租约：Worker 才是唯一抢占者"
    assert doc.error_message is None
    assert doc.source_file_path == "tenant-a/doc-1.v1.md", (
        "源文件跟着当前稿走：不换的话重解析会读到第 3 稿的字节"
    )
    assert doc.source_filename == "v3.md", "文件名只是原始上传的名字，不参与稿次语义"


@pytest.mark.asyncio
async def test_the_target_draft_without_a_source_file_clears_the_document_one(
    env: SimpleNamespace,
) -> None:
    """目标稿是 JSON 提交（没有源文件）→ 文档行的源文件路径也清掉，不留上一稿的。

    留着比清掉更坏：``reparse`` 会读到**另一稿**的字节，而那份字节看起来完全正常。

    ``source_filename`` **不跟着变**：它是文档级别的「原始上传时叫什么」，版本行只记路径，
    没有可回填的真值。它是展示用的名字，不参与「读哪份字节」。
    """

    await _rollback(env, 2)

    doc = await env.db.get(Document, DOC_ID)
    assert doc.source_file_path is None
    assert doc.source_filename == "v3.md"


@pytest.mark.asyncio
async def test_rollback_queues_a_rebuild_for_the_target_draft_after_commit(
    env: SimpleNamespace, _offline_dispatch: Any
) -> None:
    """重建载荷带**目标稿号**：Worker 不回读指针，迟到的任务就不会写错稿。

    "提交之后" 由 ``async_transaction`` 的边界证明：``_rollback`` 返回时事务已经提交，
    而投递记录此刻才在账上——反过来的顺序会让 Worker 读到一行还没提交的文档。
    """

    await _rollback(env, 1)

    index_calls, _ = _offline_dispatch
    assert [call["kwargs"]["doc_version"] for call in index_calls] == [1]
    assert index_calls[0]["kwargs"]["document_id"] == DOC_ID
    assert index_calls[0]["kwargs"]["tenant_id"] == TENANT_ID
    assert index_calls[0]["kwargs"]["kb_id"] == "kb-1"
    assert index_calls[0]["kwargs"]["reparse"] is False
    assert index_calls[0]["task_id"] == f"doc:{DOC_ID}"


@pytest.mark.asyncio
async def test_rollback_flips_the_keyword_current_draft_mark_after_commit(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键词索引的「当前稿」标记翻到目标稿：复用 #175 的既有方法，不另写一套。

    ``is_current`` 是派生视图（库里的指针才是真值），所以这一件同样排在提交之后、
    且失败只降级——这两点都由 ``app.services.keyword_draft`` 保证，这里只钉「翻到了哪一稿」。
    """

    flipped: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        flipped.append(dict(kwargs))

    monkeypatch.setattr(keyword_draft, "flip_current_draft_after_commit", _record)

    await _rollback(env, 1)

    assert flipped == [
        {"document_id": DOC_ID, "tenant_id": TENANT_ID, "doc_version": 1}
    ]


# ── 守卫：已是当前稿 / 处理中 / 稿不存在 / 跨租户 ──


@pytest.mark.asyncio
async def test_rolling_back_to_the_current_draft_is_rejected(env: SimpleNamespace) -> None:
    """回滚到当前稿 → 20032「已是当前稿」；**什么都不写**（连状态都不动）。"""

    with pytest.raises(AppError) as caught:
        await _rollback(env, 3)

    assert caught.value.code is ErrorCode.DOCUMENT_VERSION_ALREADY_ACTIVE
    assert caught.value.data == {"version": 3}

    rows = await _rows(env.db)
    assert [(row.version, row.status) for row in rows] == [
        (1, "archived"),
        (2, "archived"),
        (3, "current"),
    ]
    doc = await env.db.get(Document, DOC_ID)
    assert doc.status == DocumentStatus.SUCCESS, "已拒绝的回滚不该把文档置成处理中"


@pytest.mark.asyncio
async def test_a_processing_document_refuses_rollback(env: SimpleNamespace) -> None:
    """有效处理中（租约新鲜）→ 20025，与建版本 / 重建 / 删除同一个码。"""

    from app.db.base import now

    doc = await env.db.get(Document, DOC_ID)
    doc.status = DocumentStatus.PROCESSING
    doc.index_claimed_at = now()
    await env.db.commit()

    with pytest.raises(AppError) as caught:
        await _rollback(env, 1)

    assert caught.value.code is ErrorCode.DOCUMENT_IS_PROCESSING
    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 3, "被拒的回滚不动指针"


@pytest.mark.asyncio
async def test_a_purged_target_draft_is_not_found(env: SimpleNamespace) -> None:
    """目标稿已被保留策略物理删除 → 20030「稿不存在」，**不做墓碑**。

    #178 的保留策略删的是版本行本身；指针留在 v3 时去回滚一个已经没有行的稿，只能
    明确说「这一稿不存在」——补一条墓碑行会把「删过」变成永久数据。
    """

    env.db.expunge_all()
    row = await env.db.get(DocumentVersion, "v1")
    await env.db.delete(row)
    await env.db.commit()

    with pytest.raises(AppError) as caught:
        await _rollback(env, 1)

    assert caught.value.code is ErrorCode.DOCUMENT_VERSION_NOT_FOUND
    assert caught.value.data == {"version": 1}

    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 3
    assert doc.status == DocumentStatus.SUCCESS


@pytest.mark.asyncio
async def test_a_draft_purged_by_the_real_retention_path_is_not_found(
    env: SimpleNamespace,
) -> None:
    """真的跑一遍 #178 的清理，再回滚被清掉的那一稿 → 20030。

    上一个用例是「手动删掉一行」，这一条走**真的** ``purge_expired_versions``：两条连着
    才是「保留策略删掉的稿不能回滚」这个跨票承诺——只钉手动删除的话，清理路径哪天改成
    「留墓碑 / 留弱引用」，这里不会红。
    """

    doc = await env.db.get(Document, DOC_ID)
    async with async_transaction(env.db):
        plan = await document_version_service.purge_expired_versions(
            env.db, document=doc, retain=1, stores=[]
        )

    # 顺序不是这条用例要钉的（那是 #178 自己的契约）：只要求「这一批被清掉了」。
    assert set(plan.expired_versions) == {1, 2}
    assert [row.version for row in await _rows(env.db)] == [3]

    with pytest.raises(AppError) as caught:
        await _rollback(env, 1)

    assert caught.value.code is ErrorCode.DOCUMENT_VERSION_NOT_FOUND
    assert caught.value.data == {"version": 1}
    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 3, "被拒的回滚不动指针"
    assert doc.status == DocumentStatus.SUCCESS


@pytest.mark.asyncio
async def test_an_unknown_or_cross_tenant_document_is_not_found(env: SimpleNamespace) -> None:
    """跨租户一律「不存在」（20016）：不返回「无权限」，不泄露资源存在性。"""

    with pytest.raises(AppError) as caught:
        await _rollback(env, 1, tenant_id="tenant-b")

    assert caught.value.code is ErrorCode.NOT_FOUND
    doc = await env.db.get(Document, DOC_ID)
    assert doc.current_version == 3
