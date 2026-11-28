"""改库切块设置 + 按篇重建（第 16 步 #207 的 C 部分 / 第 17 步口径）。

这一票里最容易「看起来没问题、实际很贵」的地方就是这里：改设置会触发**整库重建**
（全库重嵌入 + 重写 ES）。所以守三件事：

1. 没确认就拒（``20002``），不落库、不投递；
2. 确认后一次落两样——设置、审计——并**逐篇**投递重建，投递登记在**提交之后**；
3. 库级构建的版本行 / 四计数 / 激活已经整层删除：回执不再有 ``build_id``，只有
   「投了多少篇、落后篇一共多少篇」。进度看每篇自己的 ``status``。

## 「全库重建」的判据是**产物落后**（不是「所有成功篇」）

这一条是现场评审抓出来的：按 ``SUCCESS`` 取最新 300 篇时，跑完的篇又原样落回「最新 300
篇」里，第 301 篇之后永远轮不到，而已建好的篇还会被重新切块重嵌入一轮。判据现在与夜间
清扫器同一份（``generation_sweep._stale_predicate``），本文件同时用**真 SQLite**钉住
「第二次点击继续往剩下的落后篇推进」这个行为。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.exceptions import AppError, ErrorCode
from app.models.knowledge_base import (
    DEFAULT_SECTION_PART_MAX_CHARS,
    KnowledgeBase,
    build_params_of,
)
from app.services import console_kb_service
from app.services.index_generation import settings_fingerprint
from app.utils.status import DocumentStatus

TENANT = "tenant_demo"
KB_ID = "kb-1"


def _kb(
    *,
    split_strategy: str = "structure",
    chunk_size: int = 512,
    section_part_max_chars: int = DEFAULT_SECTION_PART_MAX_CHARS,
) -> KnowledgeBase:
    return KnowledgeBase(
        id="kb-1",
        tenant_id="tenant_demo",
        name="Handbook",
        description=None,
        split_strategy=split_strategy,
        allowed_formats=["markdown"],
        settings={
            "chunk_size": chunk_size,
            "section_part_max_chars": section_part_max_chars,
        },
    )


def test_build_params_carry_only_the_knobs_that_change_the_artifacts() -> None:
    """构建参数只收「改了就必须重建」的那三项，名称与描述不进。

    把它们算进去的话，每篇的指纹都会永远对不上库设置——一次误报就够让人学会忽略提示。
    """

    assert build_params_of(_kb()) == {
        "split_strategy": "structure",
        "chunk_size": 512,
        "section_part_max_chars": 2000,
    }


#: ``_kb()`` 库当前设置的指纹（落后判据的「当前」那一半）。
CURRENT_HASH = settings_fingerprint(build_params_of(_kb()))
#: 只用来确认「与当前不一致」也判落后：片大小换一个值。
OTHER_HASH = settings_fingerprint(build_params_of(_kb(chunk_size=300)))


# ────────────────────────── 服务层的两道门


class _KbRepo:
    """``knowledge_base_repository`` 的替身：只回答「按 id + 租户取库」。"""

    def __init__(self, kb: KnowledgeBase | None) -> None:
        self._kb = kb

    async def get_by_id_for_tenant(self, _db: Any, kb_id: str, *, tenant_id: str) -> Any:
        if self._kb is None or self._kb.tenant_id != tenant_id:
            return None
        return self._kb


class _DispatchRecorder:
    """``_dispatch_kb_rebuild`` 的替身：只记「被叫了几次、参数对不对」，不碰仓储。"""

    def __init__(self, *, dispatched: int = 3, total: int = 3) -> None:
        self.calls: list[dict[str, Any]] = []
        self._dispatched = dispatched
        self._total = total

    async def __call__(
        self,
        _db: Any,
        kb: Any,
        *,
        tenant_id: str,
        request_id: str | None,
        raise_when_empty: bool = True,
    ) -> Any:
        self.calls.append(
            {
                "kb_id": kb.id,
                "tenant_id": tenant_id,
                "request_id": request_id,
                "raise_when_empty": raise_when_empty,
            }
        )
        return self._dispatched, self._total


@pytest.mark.asyncio
async def test_an_unconfirmed_change_is_rejected_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没确认 → 20002，且库一行没改、一篇都没投。

    这次改动的代价是整库重嵌入；「顺手改一下」不能悄悄变成那个代价。
    """

    kb = _kb()
    repo = _KbRepo(kb)
    audits: list[dict[str, Any]] = []
    dispatch = _DispatchRecorder()

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(
        console_kb_service, "append_audit", lambda *a, **kw: audits.append(kw) or _noop()
    )
    monkeypatch.setattr(console_kb_service, "_dispatch_kb_rebuild", dispatch)

    with pytest.raises(AppError) as caught:
        await console_kb_service.update_kb_settings(
            object(),  # type: ignore[arg-type]
            tenant_id="tenant_demo",
            kb_id="kb-1",
            actor=_actor(),
            request_id=None,
            split_strategy=None,
            chunk_size=None,
            section_part_max_chars=1600,
            confirmed=False,
        )

    assert caught.value.code == ErrorCode.PARAM_ERROR
    assert kb.settings["section_part_max_chars"] == 2000, "设置没被改"
    assert audits == [], "没写审计"
    assert dispatch.calls == [], "没投任何篇"


@pytest.mark.asyncio
async def test_a_confirmed_change_writes_settings_audit_and_dispatches_a_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认后：设置落库、审计一条、逐篇投递一次，回执带投递篇数。"""

    kb = _kb()
    repo = _KbRepo(kb)
    audits: list[dict[str, Any]] = []
    dispatch = _DispatchRecorder()

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(
        console_kb_service, "append_audit", lambda *a, **kw: audits.append(kw) or _noop()
    )
    monkeypatch.setattr(console_kb_service, "_dispatch_kb_rebuild", dispatch)

    data = await console_kb_service.update_kb_settings(
        object(),  # type: ignore[arg-type]
        tenant_id="tenant_demo",
        kb_id="kb-1",
        actor=_actor(),
        request_id=None,
        split_strategy=None,
        chunk_size=None,
        section_part_max_chars=1600,
        confirmed=True,
    )

    assert kb.settings["section_part_max_chars"] == 1600
    assert kb.settings["chunk_size"] == 512, "没发的字段保持原值"
    assert len(audits) == 1
    assert audits[0]["action"] == console_kb_service.ACTION_KB_SETTINGS_UPDATE
    assert audits[0]["before"]["section_part_max_chars"] == 2000
    assert audits[0]["after"]["section_part_max_chars"] == 1600
    assert dispatch.calls == [
        {
            "kb_id": "kb-1",
            "tenant_id": "tenant_demo",
            "request_id": None,
            # 改设置这条**不**因为「没有可重建的篇」而拒：空库先配好切块策略是正常操作
            # （与「点重建」刻意不同，理由见 `update_kb_settings` 的 docstring）。
            "raise_when_empty": False,
        }
    ], "一次改动只投递一批"
    assert data["documents_dispatched"] == 3
    assert data["documents_total"] == 3


@pytest.mark.asyncio
async def test_an_out_of_range_threshold_is_rejected_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """越界值在校验处就拒：不能先写设置、再让重建用一个非法阈值。"""

    kb = _kb()
    repo = _KbRepo(kb)
    dispatch = _DispatchRecorder()
    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(console_kb_service, "append_audit", lambda *a, **kw: _noop())
    monkeypatch.setattr(console_kb_service, "_dispatch_kb_rebuild", dispatch)

    with pytest.raises(AppError) as caught:
        await console_kb_service.update_kb_settings(
            object(),  # type: ignore[arg-type]
            tenant_id="tenant_demo",
            kb_id="kb-1",
            actor=_actor(),
            request_id=None,
            split_strategy=None,
            chunk_size=None,
            section_part_max_chars=1,
            confirmed=True,
        )

    assert caught.value.code == ErrorCode.PARAM_ERROR
    assert kb.settings["section_part_max_chars"] == 2000
    assert dispatch.calls == []


def _actor() -> Any:
    class _Actor:
        account_id = "acc-1"

    return _Actor()


async def _noop() -> None:
    return None


# ────────────────────────────── 投递本身：按篇、有上限、登记在提交之后


@pytest.mark.asyncio
async def test_the_dispatch_helper_prepares_and_registers_one_enqueue_per_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_dispatch_kb_rebuild`` 每篇一次「置排队中」+ 一次 after_commit 投递（投到 build 队列）。

    这是全库重建**唯一**的写路径（改设置与单纯重建都走它）：少登记一次就是一篇永远不跑，
    多登记一次就是同一篇被投两遍（CAS 会挡，但白占一条消息）。

    投递必须显式带 ``queue="build"``：批量重建不能把常规队列占满（ADR-0034 决策八），
    而「只看 kwargs 六个键」的接线用例不会发现队列悄悄退回默认——所以这里把登记在提交之后
    的那个动作真的执行一次，读它交给 ``enqueue_document`` 的实参。
    """

    documents = [
        SimpleNamespace(id="doc-0", current_version=1),
        SimpleNamespace(id="doc-1", current_version=1),
    ]

    prepared: list[str] = []
    scheduled: list[Any] = []
    enqueued: list[dict[str, Any]] = []

    async def _count(_db: Any, _kb_id: str, *, tenant_id: str, stale: Any) -> int:
        return 5

    async def _list(
        _db: Any, _kb_id: str, *, tenant_id: str, stale: Any, limit: int | None
    ) -> list[Any]:
        assert limit == console_kb_service.MAX_REBUILD_DOCUMENTS_PER_REQUEST
        return documents

    async def _prepare(_db: Any, document_id: str, *, tenant_id: str, allow_success: bool) -> bool:
        assert allow_success is True, "重建要能抢占 SUCCESS 的篇"
        prepared.append(document_id)
        return True

    def _enqueue(**kwargs: Any) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(console_kb_service.document_repository, "count_stale_for_rebuild", _count)
    monkeypatch.setattr(console_kb_service.document_repository, "list_stale_for_rebuild", _list)
    monkeypatch.setattr(
        console_kb_service.document_repository, "prepare_for_indexing_async", _prepare
    )
    monkeypatch.setattr(
        console_kb_service, "after_commit", lambda _db, thunk: scheduled.append(thunk)
    )
    monkeypatch.setattr(console_kb_service, "enqueue_document", _enqueue)

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        object(),  # type: ignore[arg-type]
        _kb(),
        tenant_id=TENANT,
        request_id=None,
    )

    for thunk in scheduled:
        await thunk()

    assert dispatched == 2
    assert total == 5, "total 是落后篇总数，不是投出去的篇数"
    assert prepared == ["doc-0", "doc-1"]
    assert [call["document_id"] for call in enqueued] == ["doc-0", "doc-1"], "每篇一个提交后动作"
    assert all(call["queue"] == console_kb_service.REBUILD_QUEUE for call in enqueued), (
        "批量重建走 build 队列：退回默认队列就与上传 / 单篇重建抢同一批槽位"
    )


@pytest.mark.asyncio
async def test_the_dispatch_helper_skips_a_document_whose_cas_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抢不到（别处刚置成排队 / 索引中）→ 跳过、不投、不计入投递数。"""

    document = SimpleNamespace(id="doc-0", current_version=1)
    scheduled: list[Any] = []

    async def _count(_db: Any, _kb_id: str, *, tenant_id: str, stale: Any) -> int:
        return 1

    async def _list(
        _db: Any, _kb_id: str, *, tenant_id: str, stale: Any, limit: int | None
    ) -> list[Any]:
        return [document]

    async def _prepare(_db: Any, document_id: str, *, tenant_id: str, allow_success: bool) -> bool:
        return False

    monkeypatch.setattr(console_kb_service.document_repository, "count_stale_for_rebuild", _count)
    monkeypatch.setattr(console_kb_service.document_repository, "list_stale_for_rebuild", _list)
    monkeypatch.setattr(
        console_kb_service.document_repository, "prepare_for_indexing_async", _prepare
    )
    monkeypatch.setattr(
        console_kb_service, "after_commit", lambda _db, thunk: scheduled.append(thunk)
    )

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        object(),  # type: ignore[arg-type]
        _kb(),
        tenant_id=TENANT,
        request_id=None,
    )

    assert (dispatched, total) == (0, 1), "抢不到 ≠ 没有落后篇：total 仍是 1"
    assert scheduled == [], "抢不到就一个字都不投"


# ──────────────── 落后判据与「第二次点击继续」：真 SQLite，跑真语句
#
# 为什么这几条要用真 SQL：判据（三条并集）与「落后集合会自己缩小」这件事在**语句**上成立，
# 换成记账替身的话，替身里那份 Python 判据才是被测的东西——而它恰恰不是生产用的那份。

#: 与 ``app/models/document.py`` 的列一一对应（ORM 的 SELECT 会把每一列都点出来）。
_DOCUMENTS_DDL = """
CREATE TABLE documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    title VARCHAR(512) NOT NULL DEFAULT 't',
    content TEXT NOT NULL DEFAULT '',
    source_type VARCHAR(32) NOT NULL DEFAULT 'markdown',
    source_file_path VARCHAR(1024),
    source_filename VARCHAR(512),
    status INTEGER NOT NULL DEFAULT 4,
    current_version INTEGER NOT NULL DEFAULT 1,
    current_index_generation INTEGER,
    settings_hash VARCHAR(64),
    index_claimed_at DATETIME,
    index_heartbeat_at DATETIME,
    redispatch_count INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """一个干净的内存库（只有 ``documents``）。"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(text(_DOCUMENTS_DDL))
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()
        await engine.dispose()


async def _seed_document(
    db: AsyncSession,
    document_id: str,
    *,
    settings_hash: str | None,
    generation: int | None,
    updated_at: str,
    status: int = int(DocumentStatus.SUCCESS),
) -> None:
    """一篇文档行：``settings_hash`` / ``generation`` 决定它落不落后，``updated_at`` 决定顺序。"""

    await db.execute(
        text(
            "INSERT INTO documents (id, tenant_id, kb_id, title, content, source_type, status, "
            "current_version, current_index_generation, settings_hash, created_at, updated_at) "
            "VALUES (:id, :tenant, :kb, 't', 'body', 'markdown', :status, 1, :generation, "
            ":hash, :updated, :updated)"
        ),
        {
            "id": document_id,
            "tenant": TENANT,
            "kb": KB_ID,
            "status": status,
            "generation": generation,
            "hash": settings_hash,
            "updated": updated_at,
        },
    )


def _record_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """登记在提交之后的投递换成记账器：真 SQL 用例要验的是「哪些篇被投」。"""

    enqueued: list[dict[str, Any]] = []
    scheduled: list[Any] = []

    def _enqueue(**kwargs: Any) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(console_kb_service, "enqueue_document", _enqueue)
    monkeypatch.setattr(
        console_kb_service, "after_commit", lambda _db, thunk: scheduled.append(thunk)
    )
    return enqueued, scheduled


async def _finish_documents(db: AsyncSession, document_ids: list[str]) -> None:
    """把投出去的篇模拟成「Worker 跑完了」：回到 SUCCESS、指纹对齐、有产物。"""

    for document_id in document_ids:
        await db.execute(
            text(
                "UPDATE documents SET status = :status, settings_hash = :hash, "
                "current_index_generation = 2 WHERE id = :id"
            ),
            {"status": int(DocumentStatus.SUCCESS), "hash": CURRENT_HASH, "id": document_id},
        )


@pytest.mark.asyncio
async def test_already_up_to_date_documents_are_never_dispatched(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已建好的篇（指纹对齐 + 有产物）一次都不投——这正是 #209 用户故事 21 要求的那条。"""

    # 两篇最新一代、三篇落后（一篇指纹对不上、一篇从没记过、一篇没有产物）。
    for index in range(2):
        await _seed_document(
            session,
            f"doc-fresh-{index}",
            settings_hash=CURRENT_HASH,
            generation=2,
            updated_at=f"2026-01-01 00:00:0{index + 1}",
        )
    for index in range(3):
        await _seed_document(
            session,
            f"doc-stale-{index}",
            settings_hash=OTHER_HASH if index != 1 else None,
            generation=1 if index != 2 else None,
            updated_at=f"2026-01-01 00:0{index + 1}:00",
        )

    enqueued, scheduled = _record_enqueues(monkeypatch)

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        session, _kb(), tenant_id=TENANT, request_id=None
    )
    for thunk in scheduled:
        await thunk()

    assert (dispatched, total) == (3, 3), "total 只数落后的 3 篇，不把已建好的 2 篇算进来"
    assert {call["document_id"] for call in enqueued} == {
        "doc-stale-0",
        "doc-stale-1",
        "doc-stale-2",
    }

    # 已建好的两篇连「置排队中」都不该经历：真 prepare 的 UPDATE 没碰它们，它们仍是 SUCCESS。
    rows = (await session.execute(text("SELECT id, status FROM documents ORDER BY id"))).all()
    statuses = {row[0]: int(row[1]) for row in rows}
    assert statuses["doc-fresh-0"] == int(DocumentStatus.SUCCESS)
    assert statuses["doc-fresh-1"] == int(DocumentStatus.SUCCESS), (
        "已建好的篇被置成排队中 = 白重算一轮 embedding"
    )


@pytest.mark.asyncio
async def test_a_second_click_continues_with_the_remaining_stale_documents(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次点击从剩下的落后篇开始，**不重投前一批**——不需要游标 / 偏移。

    第一次投 2 篇（上限压到 2），把它们模拟成「跑完且指纹对齐」；第二次投剩下的 3 篇。
    这是「最新 300 篇 + ``created_at DESC``」那个实现的直接反证：那版第二次会又选中同一批
    最新的篇，第 301 篇之后永远轮不到。
    """

    for index in range(5):
        await _seed_document(
            session,
            f"doc-{index}",
            settings_hash=OTHER_HASH,
            generation=1,
            updated_at=f"2026-01-01 00:00:0{index + 1}",
        )

    enqueued, scheduled = _record_enqueues(monkeypatch)
    monkeypatch.setattr(console_kb_service, "MAX_REBUILD_DOCUMENTS_PER_REQUEST", 2)

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        session, _kb(), tenant_id=TENANT, request_id=None
    )
    for thunk in scheduled:
        await thunk()

    assert (dispatched, total) == (2, 5), "第一批：投 2 篇，落后总数 5 篇（不是「全库成功篇数」）"
    assert [call["document_id"] for call in enqueued] == ["doc-0", "doc-1"], (
        "最旧优先：先投更新时刻最早的篇，顺序稳定"
    )

    # Worker 跑完：把前两篇变回「成功 + 指纹对齐 + 有产物」。
    await _finish_documents(session, ["doc-0", "doc-1"])

    enqueued.clear()
    scheduled.clear()
    monkeypatch.setattr(console_kb_service, "MAX_REBUILD_DOCUMENTS_PER_REQUEST", 3)

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        session, _kb(), tenant_id=TENANT, request_id=None
    )
    for thunk in scheduled:
        await thunk()

    assert (dispatched, total) == (3, 3), "第二批只该剩 3 篇落后；total 不是「库里 5 篇成功」"
    assert [call["document_id"] for call in enqueued] == ["doc-2", "doc-3", "doc-4"], (
        "第二次点击必须推进到剩下的落后篇，而不是重投前两篇"
    )
    assert all(call["queue"] == console_kb_service.REBUILD_QUEUE for call in enqueued)


@pytest.mark.asyncio
async def test_a_library_without_stale_documents_is_nothing_to_rebuild(
    session: AsyncSession,
) -> None:
    """没有落后篇时回 ``20038``，而不是看起来成功的 ``0 / 0``。

    「点了没反应」的根因就是那个空回执：前端只能弹「这次投递 0 / 0 篇」，既没有原因也没有
    下一步。空库 / 全部已最新（或全部失败、排队中）都归这一条。
    """

    await _seed_document(
        session,
        "doc-fresh",
        settings_hash=CURRENT_HASH,
        generation=2,
        updated_at="2026-01-01 00:00:01",
    )

    with pytest.raises(AppError) as caught:
        await console_kb_service._dispatch_kb_rebuild(
            session, _kb(), tenant_id=TENANT, request_id=None
        )

    assert caught.value.code == ErrorCode.KB_NOTHING_TO_REBUILD
    assert "没有需要重建" in caught.value.msg, "错误码之外还要给出一句人话的下一步"


# ───────────────────────── 「预览说 N 篇」与「点下去投 N 篇」是同一个数


@pytest.mark.asyncio
async def test_the_settings_preview_counts_the_same_stale_documents_as_a_click_would(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预览的 ``documents_to_rebuild`` 与投递回执的 ``documents_total`` 同一份判据。

    用「全部 SUCCESS 篇」会多报：已建好的篇被算进「将重建 N 篇」，而点下去一篇都不投——
    用户看到「说 12 篇、投 5 篇」之后就再也不会相信这个弹窗。
    """

    kb = _kb()
    stale_documents = [SimpleNamespace(id="doc-0"), SimpleNamespace(id="doc-1")]

    async def _list(
        _db: Any, kb_id: str, *, tenant_id: str, stale: Any, limit: int | None
    ) -> list[Any]:
        assert limit is None, "预览要看全部落后篇（投递那边才按上限切一批）"
        return stale_documents

    async def _count_chunks(_db: Any, document_ids: list[str], *, tenant_id: str) -> dict[str, int]:
        assert document_ids == ["doc-0", "doc-1"], "只加落后那批篇的切片"
        return {"doc-0": 3, "doc-1": 1}

    async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("预览已经不再取「全部 SUCCESS 篇」")

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        _KbRepo(kb).get_by_id_for_tenant,
    )
    monkeypatch.setattr(console_kb_service.document_repository, "list_stale_for_rebuild", _list)
    monkeypatch.setattr(console_kb_service.document_repository, "list_by_kb_async", _forbidden)
    monkeypatch.setattr(console_kb_service.chunk_repository, "count_by_document_ids", _count_chunks)

    data = await console_kb_service.settings_preview(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    assert data["documents_to_rebuild"] == 2
    assert data["chunks_to_rebuild"] == 4, "切片数只加落后那批，与「将重建 N 篇」同源"


# ─────────────────────────────────────── 单纯重建（第 17 步 / 现场 #226）


@pytest.mark.asyncio
async def test_rebuild_does_not_touch_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """「重建」只按当前设置投递：设置一个字段都不改，审计**单独一条**动作。

    为什么不复用改设置那条：它的 before/after 是设置快照，而这条什么都没改——写成
    `kb.settings_update` 会得到一条 before == after 的记录，读账的人会以为改坏了。
    """

    kb = _kb()
    repo = _KbRepo(kb)
    audits: list[dict[str, Any]] = []
    dispatch = _DispatchRecorder(dispatched=4, total=4)
    before_settings = dict(kb.settings)
    before_strategy = kb.split_strategy

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(
        console_kb_service, "append_audit", lambda *a, **kw: audits.append(kw) or _noop()
    )
    monkeypatch.setattr(console_kb_service, "_dispatch_kb_rebuild", dispatch)

    data = await console_kb_service.rebuild_kb(
        object(),  # type: ignore[arg-type]
        tenant_id="tenant_demo",
        kb_id="kb-1",
        actor=_actor(),
        request_id=None,
    )

    assert kb.settings == before_settings, "「重建」不改任何设置"
    assert kb.split_strategy == before_strategy
    assert dispatch.calls == [
        {
            "kb_id": "kb-1",
            "tenant_id": "tenant_demo",
            "request_id": None,
            "raise_when_empty": True,
        }
    ]
    assert len(audits) == 1
    assert audits[0]["action"] == console_kb_service.ACTION_KB_REBUILD
    assert audits[0]["target"] == "kb:kb-1"
    assert audits[0]["after"]["documents_dispatched"] == 4
    assert audits[0]["after"]["documents_total"] == 4
    assert audits[0]["after"]["batch_limit"] == console_kb_service.MAX_REBUILD_DOCUMENTS_PER_REQUEST
    assert data == {"kb_id": "kb-1", "documents_dispatched": 4, "documents_total": 4}


@pytest.mark.asyncio
async def test_rebuild_of_a_missing_knowledge_base_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失 / 跨租户的库 → 20008，且不动任何东西（不投递、不写审计）。"""

    audits: list[dict[str, Any]] = []
    dispatch = _DispatchRecorder()

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        _KbRepo(None).get_by_id_for_tenant,
    )
    monkeypatch.setattr(
        console_kb_service, "append_audit", lambda *a, **kw: audits.append(kw) or _noop()
    )
    monkeypatch.setattr(console_kb_service, "_dispatch_kb_rebuild", dispatch)

    with pytest.raises(AppError) as ctx:
        await console_kb_service.rebuild_kb(
            object(),  # type: ignore[arg-type]
            tenant_id="tenant_demo",
            kb_id="kb-1",
            actor=_actor(),
            request_id=None,
        )

    assert ctx.value.code == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert dispatch.calls == []
    assert audits == []
