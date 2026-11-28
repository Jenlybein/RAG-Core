"""控制面读模型：**库摘要 / 详情一页 / 构建摘要**（第 17 步 #227）。

## 这一票要解决的两件事

1. **列表与详情挤在一个接口里**：`/knowledge-bases/tree` 一次把整个租户的全部文档读出来，
   于是卡片（只要四个数字）与详情表（要一页文档）共用同一份太重的东西，也就没法各自定
   刷新节奏。详情因此从来没有「打开即取」这个动作——用户看到的是列表上一次轮询的旧快照。
2. **「这份文档进没进这一版」没有地方表达**：构建期间文档状态几乎一直是「成功」（每篇只有
   自己那一秒是「索引中」），所以状态列说不出「还在重建」。这里给它**第二根轴**
   （`build_state`），而不去污染文档自己的四态。

## 断言口径

* `chunk_count` 必须是「**生效版本 × 当前稿**」——不是留存版本之和（那曾是 4 倍误差）；
* `build_state` 的四种取值各有判据，逐条钉；
* 分页的 `total` 与 `documents` 必须用**同一组条件**（页数与实际行数不许打架）。

## 为什么这些用例是离线的

被测的是**口径**（哪一版、哪一根轴、哪一组条件），不是 SQL 能不能跑。真库上的行为另有
`tests/test_console_kbs.py`（端到端接线）与真跑验证覆盖；这里把仓储层换成记账替身，
就能一眼看出服务层到底把哪个参数传下去了——那正是这类 bug 的所在。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PlanSpec
from app.services import console_kb_service
from app.utils.status import DocumentStatus

TENANT = "tenant-read-model"
KB_ID = "kb-read-model"

#: 读模型只用得到 ``max_tokens_per_kb``（容量上限）与名字，其余参数给默认值即可。
PLAN = PlanSpec(
    code="free",
    name="免费版",
    max_kb=3,
    max_documents_per_kb=30,
    max_tokens_per_kb=30_000,
    chunk_daily_tokens=300_000,
    max_top_k=20,
    retrieve_allowed=True,
    rewrite_allowed=False,
    decompose_allowed=False,
    research_allowed=False,
    rerank_allowed=False,
    retrieve_qps=1,
    retrieve_daily=1000,
    rerank_qps=1,
    rerank_daily=1000,
    llm_qps=1,
    llm_daily=1000,
    max_kb_per_retrieve=3,
)


def _kb(**overrides: Any) -> KnowledgeBase:
    values: dict[str, Any] = {
        "id": KB_ID,
        "tenant_id": TENANT,
        "name": "读模型库",
        "description": None,
        "split_strategy": "structure",
        "allowed_formats": ["markdown"],
        "settings": {"chunk_size": 512, "section_part_max_chars": 2000},
        "created_at": datetime(2026, 1, 1, 9, 0, 0),
        "active_index_version": "v2",
    }
    values.update(overrides)
    return KnowledgeBase(**values)


def _document(
    document_id: str,
    *,
    status: int = DocumentStatus.SUCCESS,
    title: str | None = None,
    current_version: int = 1,
    token_count: int = 100,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=document_id,
        tenant_id=TENANT,
        kb_id=KB_ID,
        title=title or f"{document_id}.md",
        source_type="markdown",
        status=status,
        error_message=None if status == DocumentStatus.SUCCESS else "boom",
        current_version=current_version,
        token_count=token_count,
        created_at=datetime(2026, 1, 2, 9, 0, 0),
    )


def _build(
    *,
    status: str = INDEX_BUILD_BUILDING,
    version: str = "v2",
    planned: int = 3,
    done: int = 1,
    failed: list[dict[str, Any]] | None = None,
    doc_version: int | None = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="build-1",
        tenant_id=TENANT,
        kb_id=KB_ID,
        version=version,
        status=status,
        documents_planned=planned,
        documents_done=done,
        doc_version=doc_version,
        failed_documents=failed or [],
        created_at=datetime(2026, 1, 2, 10, 0, 0),
    )


class _Recorder:
    """记下服务层把哪些参数交给了仓储（这份记录就是本文件的断言对象）。"""

    def __init__(self) -> None:
        self.count_in_kb: list[dict[str, Any]] = []
        self.count_by_document_ids: list[dict[str, Any]] = []
        self.page: list[dict[str, Any]] = []
        self.filtered_total: list[dict[str, Any]] = []
        self.listed_versions: list[dict[str, Any]] = []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kb: KnowledgeBase | None,
    #: 「N 篇待更新」的读数（现场九·第三步）；判据本身用真 SQL 在另一处验。
    stale_documents: int = 0,
    documents: list[Any] | None = None,
    build: SimpleNamespace | None = None,
    done_ids: set[str] | None = None,
    chunk_totals: dict[str, int] | None = None,
    kb_chunk_total: int = 0,
    filtered_total: int | None = None,
) -> _Recorder:
    """把服务依赖的仓储全换成记账替身（不连库、不连 Redis）。"""

    recorder = _Recorder()
    rows = documents or []
    chunks = chunk_totals or {}
    # `find_active` 的替身：库行记着 v2 时，生效版本行就是这一份（`params` 与当前设置一致）。
    active_row = SimpleNamespace(
        version=getattr(kb, "active_index_version", None),
        params={
            "chunk_size": 512,
            "split_strategy": "structure",
            "section_part_max_chars": 2000,
        },
    )
    state = {"build": build, "active_row": active_row if kb is not None else None}

    async def _get_kb(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return kb if kb is not None and kb.tenant_id == tenant_id else None

    async def _list_by_tenant(_db: Any, tenant_id: str) -> list[Any]:
        return [kb] if kb is not None else []

    async def _count_by_kb(_db: Any, kb_id: str, *, tenant_id: str) -> int:
        return len(rows)

    async def _count_by_status(_db: Any, kb_id: str, *, tenant_id: str) -> dict[int, int]:
        tally: dict[int, int] = {}
        for row in rows:
            tally[int(row.status)] = tally.get(int(row.status), 0) + 1
        return tally

    async def _count_in_kb(_db: Any, kb_id: str, *, tenant_id: str) -> int:
        recorder.count_in_kb.append({"kb_id": kb_id})
        return kb_chunk_total

    async def _count_by_document_ids(
        _db: Any, document_ids: list[str], *, tenant_id: str
    ) -> dict[str, int]:
        recorder.count_by_document_ids.append({"document_ids": list(document_ids)})
        return {document_id: chunks.get(document_id, 0) for document_id in document_ids}

    async def _list_page(
        _db: Any,
        kb_id: str,
        *,
        tenant_id: str,
        offset: int = 0,
        limit: int = 50,
        status: int | None = None,
        keyword: str | None = None,
    ) -> list[Any]:
        recorder.page.append(
            {"offset": offset, "limit": limit, "status": status, "keyword": keyword}
        )
        return rows[offset : offset + limit]

    async def _count_filtered(
        _db: Any,
        kb_id: str,
        *,
        tenant_id: str,
        status: int | None = None,
        keyword: str | None = None,
    ) -> int:
        recorder.filtered_total.append({"status": status, "keyword": keyword})
        return len(rows) if filtered_total is None else filtered_total

    async def _latest(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return state["build"]

    async def _get_build(_db: Any, build_id: str, *, tenant_id: str) -> Any:
        return state["build"]

    async def _find_active(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return state["active_row"]

    async def _done_ids(
        _db: Any, kb_id: str, *, tenant_id: str, index_version: str, doc_version: int | None
    ) -> set[str]:
        # 构建真值查询（``list_indexed_document_ids``）仍然按「某一版 × 那一稿」问——
        # 它问的不是「现在能召回什么」，而是「这一版建到第几篇了」。
        del index_version, doc_version
        return set(done_ids or ())

    async def _count_tokens(_db: Any, kb_id: str, *, tenant_id: str) -> int:
        return 1234

    async def _settings_fingerprint(_db: Any, kb_id: str, *, tenant_id: str) -> str | None:
        # 库当前设置的指纹（现场九）：这一层只负责取，判据在真 SQL 那边验。
        return None

    async def _count_stale(
        _db: Any, kb_id: str, *, tenant_id: str, settings_hash: str | None
    ) -> int:
        # 「N 篇待更新」（现场九·第三步）：服务层只是把它搬出去，判据本身在
        # ``tests/test_index_generation_stale.py`` 里用真 SQL 验。
        return stale_documents

    async def _plan(_db: Any, _plan_id: int) -> PlanSpec:
        return PLAN

    async def _plan_id(_db: Any, *, tenant_id: str) -> int:
        return 1

    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository, "get_by_id_for_tenant", _get_kb
    )
    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository, "list_by_tenant", _list_by_tenant
    )
    monkeypatch.setattr(console_kb_service.document_repository, "count_by_kb", _count_by_kb)
    monkeypatch.setattr(
        console_kb_service.document_repository, "count_by_kb_by_status", _count_by_status
    )
    monkeypatch.setattr(console_kb_service.document_repository, "count_tokens_in_kb", _count_tokens)
    monkeypatch.setattr(
        console_kb_service.document_repository, "count_stale_generations", _count_stale
    )
    monkeypatch.setattr(
        console_kb_service.knowledge_base_repository,
        "get_settings_fingerprint",
        _settings_fingerprint,
    )
    monkeypatch.setattr(console_kb_service.document_repository, "list_page_by_kb", _list_page)
    monkeypatch.setattr(
        console_kb_service.document_repository, "count_by_kb_filtered", _count_filtered
    )
    monkeypatch.setattr(console_kb_service.chunk_repository, "count_in_kb", _count_in_kb)
    monkeypatch.setattr(
        console_kb_service.chunk_repository, "count_by_document_ids", _count_by_document_ids
    )
    monkeypatch.setattr(
        console_kb_service.chunk_repository, "list_document_ids_in_version", _done_ids
    )
    monkeypatch.setattr(console_kb_service.index_version_repository, "latest_for_kb", _latest)
    monkeypatch.setattr(console_kb_service.index_version_repository, "get_for_tenant", _get_build)
    monkeypatch.setattr(console_kb_service.index_version_repository, "find_active", _find_active)
    monkeypatch.setattr(console_kb_service.plan_service, "resolve_plan", _plan)
    monkeypatch.setattr(console_kb_service, "_tenant_plan_id", _plan_id)
    return recorder


@pytest.mark.asyncio
async def test_the_list_never_reads_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """列表只读摘要：**一次文档行都不读**。

    这一条是整个拆分的验收：卡片只要四个数字，而一个 5000 篇的库把 5000 行读出来再丢掉，
    正是「列表与详情被迫共用一份数据」的来源。
    """

    recorder = _install(monkeypatch, kb=_kb(), kb_chunk_total=42)

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert recorder.page == [], "列表不该读文档行"
    assert data["items"][0].chunk_count == 42
    assert data["items"][0].document_count == 0
    assert data["generated_at"], "响应必须带服务端时间戳（界面显示「更新于几点」）"


@pytest.mark.asyncio
async def test_the_kb_chunk_total_is_read_under_the_current_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """卡片上的合计与表格里每行共用同一个谓词（当前代 × 当前稿）。

    它**不再**锁库级生效版本（现场九·第四步）：那一条一旦回来，构建期间刚建好的那篇
    就会从合计里消失。
    """

    recorder = _install(monkeypatch, kb=_kb(active_index_version="v7"))

    await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert recorder.count_in_kb == [{"kb_id": KB_ID}], (
        "计数只看库：版本不再是可见性判据（现场九·第四步）"
    )


@pytest.mark.asyncio
async def test_a_kb_without_an_active_version_still_counts_under_the_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有生效版本（新建库 / 只写了文件）时照样查——可见性只看「代 × 稿」。

    改造前这里会传 ``None`` 并跳过查询；现在没有库级版本可传了，空的库自然回 0。
    """

    recorder = _install(monkeypatch, kb=_kb(active_index_version=None, settings={}))

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert recorder.count_in_kb == [{"kb_id": KB_ID}]
    assert data["items"][0].chunk_count == 0
    assert data["items"][0].needs_rebuild is True, "没有生效版本 = 一定还需要重建"


@pytest.mark.asyncio
async def test_the_build_summary_carries_the_status_and_the_failure_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建摘要带 ``status``：详情页靠它区分「还在跑」与「已经收尾」。

    只有进度时（``BuildProgressData``）构建一收尾界面就失去上下文——而实测 10 篇的库
    9.5 秒就建完，用户点进详情时那次构建早就结束了。
    """

    _install(
        monkeypatch,
        kb=_kb(),
        build=_build(
            status=INDEX_BUILD_FAILED,
            planned=10,
            done=10,
            failed=[{"document_id": "doc-3", "title": "doc-3.md", "reason": "boom"}],
        ),
    )

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    build = data["items"][0].build
    assert build is not None
    assert build.status == INDEX_BUILD_FAILED
    assert build.planned == 10
    assert build.done == 10
    assert build.remaining == 0
    assert build.failure_count == 1
    assert build.failures[0].document_id == "doc-3"
    assert build.created_at is not None


@pytest.mark.asyncio
async def test_a_kb_that_never_built_reports_no_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """从没建过：``build`` 是 ``None``，不是一条空白构建。"""

    _install(monkeypatch, kb=_kb(), build=None)

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert data["items"][0].build is None


@pytest.mark.asyncio
async def test_the_documents_page_slices_by_page_and_uses_the_same_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第 3 页 = offset 40 / limit 20，且 ``total`` 与这一页用同一组条件。"""

    documents = [_document(f"doc-{index}") for index in range(45)]
    recorder = _install(monkeypatch, kb=_kb(), documents=documents, filtered_total=45)

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
        page=3,
        page_size=20,
        status=DocumentStatus.FAILED,
        keyword="policy",
    )

    assert recorder.page == [
        {"offset": 40, "limit": 20, "status": DocumentStatus.FAILED, "keyword": "policy"}
    ]
    assert recorder.filtered_total == [{"status": DocumentStatus.FAILED, "keyword": "policy"}]
    assert data["total"] == 45
    assert data["page"] == 3
    assert data["page_size"] == 20
    ids = [row.document_id for row in data["documents"]]
    assert ids == [f"doc-{index}" for index in range(40, 45)], (
        "45 行、每页 20：第 3 页只剩 5 行（offset 40），而 total 仍是 45"
    )


@pytest.mark.asyncio
async def test_a_page_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``page_size`` 有上限：一次响应不许被一个巨大的参数做成几 MB。"""

    recorder = _install(monkeypatch, kb=_kb())

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
        page=0,
        page_size=10_000,
    )

    assert data["page"] == 1, "页码下界是 1（0 与负数都当第一页）"
    assert data["page_size"] == console_kb_service.MAX_DOCUMENTS_PAGE_SIZE
    assert recorder.page[0]["limit"] == console_kb_service.MAX_DOCUMENTS_PAGE_SIZE


@pytest.mark.asyncio
async def test_every_row_counts_chunks_under_the_active_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每一行的 ``chunk_count`` 也锁生效版本（与卡片合计同一个谓词）。"""

    documents = [_document("doc-1"), _document("doc-2")]
    recorder = _install(
        monkeypatch,
        kb=_kb(active_index_version="v9"),
        documents=documents,
        chunk_totals={"doc-1": 7},
    )

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    assert recorder.count_by_document_ids == [{"document_ids": ["doc-1", "doc-2"]}]
    counts = {row.document_id: row.chunk_count for row in data["documents"]}
    assert counts == {"doc-1": 7, "doc-2": 0}, "没有切片的文档回落 0"


@pytest.mark.asyncio
async def test_build_state_is_the_second_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_state`` 四种取值各按自己的判据给：

    * 在失败明细里 → ``failed``；
    * 在这一版真值里 → ``done``；
    * 本该参与（成功）却不在真值里 → ``pending``；
    * 本来就不参与（失败 / 排队中）→ ``None``（说「待重建」是误导）。
    """

    documents = [
        _document("doc-done"),
        _document("doc-pending"),
        _document("doc-failed-in-build"),
        _document("doc-own-failure", status=DocumentStatus.FAILED),
    ]
    _install(
        monkeypatch,
        kb=_kb(),
        documents=documents,
        build=_build(
            failed=[
                {"document_id": "doc-failed-in-build", "title": "x.md", "reason": "boom"},
            ]
        ),
        done_ids={"doc-done"},
    )

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    states = {row.document_id: row.build_state for row in data["documents"]}
    assert states == {
        "doc-done": "done",
        "doc-pending": "pending",
        "doc-failed-in-build": "failed",
        "doc-own-failure": None,
    }


@pytest.mark.asyncio
async def test_build_state_is_none_when_the_kb_never_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有构建可参照时不编造第二根轴（否则每一行都显示「待重建」）。"""

    _install(monkeypatch, kb=_kb(), documents=[_document("doc-1")], build=None)

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    assert data["documents"][0].build_state is None
    assert data["build"] is None


@pytest.mark.asyncio
async def test_the_page_carries_the_kb_summary_so_the_dialog_needs_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """详情页一次拿全：库摘要（header）+ 构建（进度条）+ 一页文档。

    分三次请求会造出三个「更新于几点」互相打架的中间态。
    """

    _install(
        monkeypatch,
        kb=_kb(name="一次拿全"),
        documents=[_document("doc-1")],
        build=_build(status=INDEX_BUILD_ACTIVE),
        kb_chunk_total=12,
    )

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    assert data["kb"].name == "一次拿全"
    assert data["kb"].chunk_count == 12
    assert data["kb"].build is not None
    assert data["kb"].build.status == INDEX_BUILD_ACTIVE
    assert data["documents"][0].document_id == "doc-1"
    assert data["generated_at"]


@pytest.mark.asyncio
async def test_a_missing_kb_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """库不存在 / 跨租户 → 20008（不泄露存在性），且不读文档。"""

    from app.core.exceptions import AppError, ErrorCode

    recorder = _install(monkeypatch, kb=None)

    with pytest.raises(AppError) as raised:
        await console_kb_service.list_documents_page(
            object(),  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB_ID,
        )

    assert raised.value.code == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert recorder.page == [], "库都没有，不该再去读它的文档"


@pytest.mark.asyncio
async def test_the_summary_keeps_the_four_status_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """四态各有几篇留在摘要上。

    拆掉文档数组之后，卡片就只剩聚合数了——如果连状态计数也不回，「这个库里有 3 篇失败」
    与「有文档在跑」这两件事在列表页就彻底消失，用户只能逐个点进去看。
    """

    documents = [
        _document("doc-ok"),
        _document("doc-bad", status=DocumentStatus.FAILED),
        _document("doc-running", status=DocumentStatus.PROCESSING),
        _document("doc-queued", status=DocumentStatus.QUEUED),
    ]
    _install(monkeypatch, kb=_kb(), documents=documents)

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    summary = data["items"][0]
    assert summary.document_count == 4
    assert summary.status_counts.success == 1
    assert summary.status_counts.failed == 1
    assert summary.status_counts.processing == 1
    assert summary.status_counts.queued == 1


@pytest.mark.asyncio
async def test_a_failure_beyond_the_display_truncation_is_still_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败明细被截断（界面只回前 5 条）时，第 6 篇仍要显示「未入版」而不是「待重建」。

    ## 为什么这条必须单独钉

    ``build.failures`` 是**给界面看的**截断列表（否则一次 500 篇失败的构建会把响应撑到几 MB）。
    如果拿它当判据，第 6 篇以后的失败会一律落到 ``pending``——一个永远不会动的「待重建」。
    用户会一直等，而构建早就结束了。所以判据读构建行的**全量**失败明细。

    这条同时钉住「两处读同一行」：展示用的那份可以截断，判据用的那份不行。
    """

    documents = [_document(f"doc-{index}") for index in range(1, 8)]
    full_failures = [
        {"document_id": f"doc-{index}", "title": f"doc-{index}.md", "reason": "boom"}
        for index in range(1, 8)
    ]
    _install(
        monkeypatch,
        kb=_kb(),
        documents=documents,
        build=_build(failed=full_failures[: full_failures.__len__()]),
    )

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    # 展示那一份被截到前 5 条
    build = data["build"]
    assert build is not None
    assert len(build.failures) == 5
    assert build.failure_count == 7

    # 判据那一份是全量：第 6、7 篇也是 failed，不是 pending
    states = {row.document_id: row.build_state for row in data["documents"]}
    assert states["doc-6"] == "failed"
    assert states["doc-7"] == "failed"
