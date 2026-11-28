"""改库切块设置 + 按篇重建（第 16 步 #207 的 C 部分 / 第 17 步口径）。

这一票里最容易「看起来没问题、实际很贵」的地方就是这里：改设置会触发**整库重建**
（全库重嵌入 + 重写 ES）。所以守三件事：

1. 没确认就拒（``20002``），不落库、不投递；
2. 确认后一次落两样——设置、审计——并**逐篇**投递重建，投递登记在**提交之后**；
3. 库级构建的版本行 / 四计数 / 激活已经整层删除：回执不再有 ``build_id``，只有
   「投了多少篇、库里一共多少篇」。进度看每篇自己的 ``status``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.models.knowledge_base import (
    DEFAULT_SECTION_PART_MAX_CHARS,
    KnowledgeBase,
    build_params_of,
)
from app.services import console_kb_service


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

    async def __call__(self, _db: Any, kb: Any, *, tenant_id: str, request_id: str | None) -> Any:
        self.calls.append({"kb_id": kb.id, "tenant_id": tenant_id, "request_id": request_id})
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
    assert dispatch.calls == [{"kb_id": "kb-1", "tenant_id": "tenant_demo", "request_id": None}], (
        "一次改动只投递一批"
    )
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
    """``_dispatch_kb_rebuild`` 每篇一次「置排队中」+ 一次 after_commit 投递。

    这是全库重建**唯一**的写路径（改设置与单纯重建都走它）：少登记一次就是一篇永远不跑，
    多登记一次就是同一篇被投两遍（CAS 会挡，但白占一条消息）。
    """

    documents = [
        SimpleNamespace(id="doc-0", current_version=1),
        SimpleNamespace(id="doc-1", current_version=1),
    ]

    prepared: list[str] = []
    scheduled: list[Any] = []

    async def _count(_db: Any, _kb_id: str, *, tenant_id: str, status: int | None = None) -> int:
        return 5

    async def _list(_db: Any, _kb_id: str, *, tenant_id: str, offset: int, limit: int, status: Any):
        assert limit == console_kb_service.MAX_REBUILD_DOCUMENTS_PER_REQUEST
        return documents

    async def _prepare(_db: Any, document_id: str, *, tenant_id: str, allow_success: bool) -> bool:
        assert allow_success is True, "重建要能抢占 SUCCESS 的篇"
        prepared.append(document_id)
        return True

    monkeypatch.setattr(console_kb_service.document_repository, "count_by_kb_filtered", _count)
    monkeypatch.setattr(console_kb_service.document_repository, "list_page_by_kb", _list)
    monkeypatch.setattr(
        console_kb_service.document_repository, "prepare_for_indexing_async", _prepare
    )
    monkeypatch.setattr(
        console_kb_service, "after_commit", lambda _db, thunk: scheduled.append(thunk)
    )

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        object(),  # type: ignore[arg-type]
        _kb(),
        tenant_id="tenant_demo",
        request_id=None,
    )

    assert dispatched == 2
    assert total == 5, "库里有 5 篇，本次只投上限内能投的那些"
    assert prepared == ["doc-0", "doc-1"]
    assert len(scheduled) == 2, "每篇一个提交后动作"


@pytest.mark.asyncio
async def test_the_dispatch_helper_skips_a_document_whose_cas_was_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抢不到（别处刚置成排队 / 索引中）→ 跳过、不投、不计入投递数。"""

    document = SimpleNamespace(id="doc-0", current_version=1)
    scheduled: list[Any] = []

    async def _count(_db: Any, _kb_id: str, *, tenant_id: str, status: int | None = None) -> int:
        return 1

    async def _list(_db: Any, _kb_id: str, *, tenant_id: str, offset: int, limit: int, status: Any):
        return [document]

    async def _prepare(_db: Any, document_id: str, *, tenant_id: str, allow_success: bool) -> bool:
        return False

    monkeypatch.setattr(console_kb_service.document_repository, "count_by_kb_filtered", _count)
    monkeypatch.setattr(console_kb_service.document_repository, "list_page_by_kb", _list)
    monkeypatch.setattr(
        console_kb_service.document_repository, "prepare_for_indexing_async", _prepare
    )
    monkeypatch.setattr(
        console_kb_service, "after_commit", lambda _db, thunk: scheduled.append(thunk)
    )

    dispatched, total = await console_kb_service._dispatch_kb_rebuild(
        object(),  # type: ignore[arg-type]
        _kb(),
        tenant_id="tenant_demo",
        request_id=None,
    )

    assert (dispatched, total) == (0, 1)
    assert scheduled == [], "抢不到就一个字都不投"


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
    assert dispatch.calls == [{"kb_id": "kb-1", "tenant_id": "tenant_demo", "request_id": None}]
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
