"""改库切块设置 + 自动重建（第 16 步 #207 的 C 部分）。

这一票里最容易「看起来没问题、实际很贵」的地方就是这里：改设置会触发**整库重建**
（全库重嵌入 + 重写 ES）。所以守三件事：

1. 没确认就拒（``20002``），不落库、不起构建；
2. 确认后一次落三样——设置、审计、构建行——且构建的投递登记在**提交之后**；
3. 构建参数留档（``index_build_versions.params``），库当前设置与生效版本不一致时
   能判出「需重建」，而不是靠人记忆。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.models.knowledge_base import (
    DEFAULT_SECTION_PART_MAX_CHARS,
    KnowledgeBase,
    build_params_of,
    needs_rebuild,
)
from app.services import console_kb_service


def _kb(
    *,
    split_strategy: str = "structure",
    chunk_size: int = 512,
    section_part_max_chars: int = DEFAULT_SECTION_PART_MAX_CHARS,
    active_index_version: str | None = "v1",
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
        active_index_version=active_index_version,
    )


def test_build_params_carry_only_the_knobs_that_change_the_artifacts() -> None:
    """构建参数只收「改了就必须重建」的那三项，名称与描述不进。

    把它们算进去的话，每个库都会永远显示「需重建」——一次误报就够让人学会忽略提示。
    """

    assert build_params_of(_kb()) == {
        "split_strategy": "structure",
        "chunk_size": 512,
        "section_part_max_chars": 2000,
    }


def test_a_version_without_params_counts_as_needing_a_rebuild() -> None:
    """老版本行没留参数（``params`` 为 NULL）→ 判「需重建」。

    宁可多提示一次：猜一个「大概就是当时的默认值」写进去，会让该重建的库显示成已就绪，
    而症状是「我明明改了阈值，怎么还是整节」。
    """

    assert needs_rebuild(_kb(), None) is True
    assert needs_rebuild(_kb(), {}) is True


def test_matching_params_do_not_ask_for_a_rebuild() -> None:
    kb = _kb()
    assert needs_rebuild(kb, build_params_of(kb)) is False


def test_changing_the_section_threshold_asks_for_a_rebuild() -> None:
    """阈值是索引期旋钮：改了而版本参数还是旧的 → 必须重建才生效。"""

    kb = _kb(section_part_max_chars=1600)
    assert (
        needs_rebuild(
            kb, {"split_strategy": "structure", "chunk_size": 512, "section_part_max_chars": 2000}
        )
        is True
    )


# ────────────────────────── 服务层的两道门


class _KbRepo:
    """``knowledge_base_repository`` 的替身：只回答「按 id + 租户取库」。"""

    def __init__(self, kb: KnowledgeBase | None) -> None:
        self._kb = kb

    async def get_by_id_for_tenant(self, _db: Any, kb_id: str, *, tenant_id: str) -> Any:
        if self._kb is None or self._kb.tenant_id != tenant_id:
            return None
        return self._kb


class _FakeBuild:
    id = "build-1"
    version = "v2"


class _FakeBuildService:
    """``IndexBuildService`` 的替身：只回答「起了一个构建」，不真连库/ES。"""

    calls: list[dict[str, Any]] = []

    async def start_build(
        self,
        _db: Any,
        *,
        kb_id: str,
        tenant_id: str,
        version: str | None = None,
        auto_activate: bool = False,
    ) -> Any:
        _FakeBuildService.calls.append(
            {"kb_id": kb_id, "tenant_id": tenant_id, "auto_activate": auto_activate}
        )
        return _FakeBuild()


@pytest.mark.asyncio
async def test_an_unconfirmed_change_is_rejected_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没确认 → 20002，且库一行没改、构建一次没起。

    这次改动的代价是整库重嵌入；「顺手改一下」不能悄悄变成那个代价。
    """

    kb = _kb()
    repo = _KbRepo(kb)
    audits: list[dict[str, Any]] = []
    _FakeBuildService.calls = []

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(
        console_kb_service, "append_audit", lambda *a, **kw: audits.append(kw) or _noop()
    )
    monkeypatch.setattr(console_kb_service, "IndexBuildService", _FakeBuildService)

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
    assert _FakeBuildService.calls == [], "没起构建"


@pytest.mark.asyncio
async def test_a_confirmed_change_writes_settings_audit_and_starts_one_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认后：设置落库、审计一条、构建起一个，且投递登记在提交之后。"""

    kb = _kb()
    repo = _KbRepo(kb)
    audits: list[dict[str, Any]] = []
    scheduled: list[Any] = []
    _FakeBuildService.calls = []

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(
        console_kb_service, "append_audit", lambda *a, **kw: audits.append(kw) or _noop()
    )
    monkeypatch.setattr(console_kb_service, "IndexBuildService", _FakeBuildService)
    monkeypatch.setattr(
        console_kb_service, "after_commit", lambda _db, thunk: scheduled.append(thunk)
    )

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
    assert _FakeBuildService.calls == [
        # 改设置这条路径**要**自动激活（#215 的回归修法：布尔量记在构建行上，不靠载荷传）
        {"kb_id": "kb-1", "tenant_id": "tenant_demo", "auto_activate": True}
    ]
    assert data["build_id"] == "build-1"
    assert len(scheduled) == 1, "投递登记在提交之后，不是当场发"


@pytest.mark.asyncio
async def test_an_out_of_range_threshold_is_rejected_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """越界值在校验处就拒：不能先写设置、再让构建用一个非法阈值。"""

    kb = _kb()
    repo = _KbRepo(kb)
    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(console_kb_service, "append_audit", lambda *a, **kw: _noop())

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


def _actor() -> Any:
    class _Actor:
        account_id = "acc-1"

    return _Actor()


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_the_rebuild_is_enqueued_with_auto_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改设置起的那个构建**带激活**（第 16 步 #207）。

    这是定稿里那句「完成自动激活」的落点：改完设置停在 ``ready`` 等于把「设置页已保存」
    变成半句话，用户还得再点一次「激活」才知道生效。常规重建不这样（ADR-0025 让
    ``ready`` 是运维可见的一步），所以这条必须只对「改设置」这条路径成立。
    """

    kb = _kb()
    repo = _KbRepo(kb)
    thunks: list[Any] = []
    enqueued: list[dict[str, Any]] = []
    _FakeBuildService.calls = []

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_by_id_for_tenant",
        repo.get_by_id_for_tenant,
    )
    monkeypatch.setattr(console_kb_service, "append_audit", lambda *a, **kw: _noop())
    monkeypatch.setattr(console_kb_service, "IndexBuildService", _FakeBuildService)

    async def _fake_enqueue(**kwargs: Any) -> None:
        enqueued.append(kwargs)

    monkeypatch.setattr(console_kb_service, "after_commit", lambda _db, thunk: thunks.append(thunk))
    monkeypatch.setattr(console_kb_service, "enqueue_index_build_after_commit", _fake_enqueue)

    await console_kb_service.update_kb_settings(
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

    assert len(thunks) == 1, "投递登记在 after_commit 上，不是当场发"
    # 执行那个回调：它才是真正把任务投出去的那一步。
    await asyncio.wait_for(thunks[0](), timeout=5)
    assert enqueued == [
        {
            "publish": console_kb_service.enqueue_index_build,
            "build_id": "build-1",
            "tenant_id": "tenant_demo",
            "request_id": None,
            # 这里**不再**有 ``activate``：它记在构建行上（``start_build(auto_activate=True)``，
            # 见上一个用例），而载荷里带一个「决定激活与否」的布尔量在改成按篇投递时断过一次。
            # 本用例现在守的是「投递仍然只登记一次、且走 after_commit」。
        }
    ]


def test_a_build_that_rebuilt_nothing_is_refused() -> None:
    """库里有文档、却一篇都没重建成功 → 构建立刻失败，绝不产出一个「空版本」（#207）。

    空版本的四项计数自洽（0=0=0=0），能一路通过计数闸门变成 ``ready``；它一旦被激活，
    检索会诚实地返回空结果，而读起来像「这个库没内容」。症状不报错、只是全空。
    """

    from app.services.index_build_service import (
        BUILD_NOTHING_TO_REBUILD_MESSAGE,
        IndexBuildNothingToRebuild,
    )

    error = IndexBuildNothingToRebuild()

    assert error.msg == BUILD_NOTHING_TO_REBUILD_MESSAGE
    # 与「计数对不上」是两个码：前者是数据卡住，后者是写坏了。
    from app.services.index_build_service import BUILD_COUNTS_MISMATCH_MESSAGE

    assert error.msg != BUILD_COUNTS_MISMATCH_MESSAGE
