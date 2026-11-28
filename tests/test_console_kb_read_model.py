"""控制面读模型：**库摘要 / 详情一页**（第 17 步 #227）。

## 这一票要解决的两件事

1. **列表与详情挤在一个接口里**：`/knowledge-bases/tree` 一次把整个租户的全部文档读出来，
   于是卡片（只要四个数字）与详情表（要一页文档）共用同一份太重的东西，也就没法各自定
   刷新节奏。详情因此从来没有「打开即取」这个动作——用户看到的是列表上一次轮询的旧快照。
2. **「这一篇是不是最新的」要有地方说**：库级构建的第二根轴（`build_state`）随版本层删除，
   按篇的答案只剩一个数——`stale_documents`（「N 篇待更新」，比设置指纹）。

## 断言口径

* `chunk_count` 必须是「**当前代 × 当前稿**」——不是留存代之和（那曾是 4 倍误差）；
* 「N 篇待更新」的读数原样搬到摘要上，判据本身用真 SQL 在别处验；
* 分页的 `total` 与 `documents` 必须用**同一组条件**（页数与实际行数不许打架）。

## 为什么这些用例是离线的

被测的是**口径**（哪一代、哪一组条件），不是 SQL 能不能跑。真库上的行为另有
`tests/test_console_kbs.py`（端到端接线）与 `tests/test_console_kb_read_model_sql.py` 覆盖；
这里把仓储层换成记账替身，就能一眼看出服务层到底把哪个参数传下去了——那正是这类 bug 的
所在。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

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


class _Recorder:
    """记下服务层把哪些参数交给了仓储（这份记录就是本文件的断言对象）。"""

    def __init__(self) -> None:
        self.count_in_kb: list[dict[str, Any]] = []
        self.count_by_document_ids: list[dict[str, Any]] = []
        self.page: list[dict[str, Any]] = []
        self.filtered_total: list[dict[str, Any]] = []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kb: KnowledgeBase | None,
    #: 「N 篇待更新」的读数（现场九·第三步）；判据本身用真 SQL 在别处验。
    stale_documents: int = 0,
    documents: list[Any] | None = None,
    chunk_totals: dict[str, int] | None = None,
    kb_chunk_total: int = 0,
    filtered_total: int | None = None,
) -> _Recorder:
    """把服务依赖的仓储全换成记账替身（不连库、不连 Redis）。"""

    recorder = _Recorder()
    rows = documents or []
    chunks = chunk_totals or {}

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

    async def _count_tokens(_db: Any, kb_id: str, *, tenant_id: str) -> int:
        return 1234

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
    monkeypatch.setattr(console_kb_service.document_repository, "list_page_by_kb", _list_page)
    monkeypatch.setattr(
        console_kb_service.document_repository, "count_by_kb_filtered", _count_filtered
    )
    monkeypatch.setattr(console_kb_service.chunk_repository, "count_in_kb", _count_in_kb)
    monkeypatch.setattr(
        console_kb_service.chunk_repository, "count_by_document_ids", _count_by_document_ids
    )
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

    它**只**看库（现场九·第四步）：库级生效版本层已经删除，版本不再是可见性判据——那一条
    一旦回来，构建期间刚建好的那篇就会从合计里消失。
    """

    recorder = _install(monkeypatch, kb=_kb())

    await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert recorder.count_in_kb == [{"kb_id": KB_ID}], "计数只看库"


@pytest.mark.asyncio
async def test_a_kb_without_settings_still_counts_under_the_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置是空对象（新建库 / 只写了文件）时照样查——可见性只看「代 × 稿」。

    改造前这里会因为「没有生效版本」被特殊处理；现在没有库级版本可传了，空的库自然回 0。
    """

    recorder = _install(monkeypatch, kb=_kb(settings={}))

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert recorder.count_in_kb == [{"kb_id": KB_ID}]
    assert data["items"][0].chunk_count == 0


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
async def test_every_row_counts_chunks_under_the_current_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每一行的 ``chunk_count`` 与卡片合计用同一个谓词（当前代 × 当前稿）。"""

    documents = [_document("doc-1"), _document("doc-2")]
    recorder = _install(
        monkeypatch,
        kb=_kb(),
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
async def test_the_page_carries_the_kb_summary_so_the_dialog_needs_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """详情页一次拿全：库摘要（header）+ 一页文档。

    分三次请求会造出三个「更新于几点」互相打架的中间态。
    """

    _install(
        monkeypatch,
        kb=_kb(name="一次拿全"),
        documents=[_document("doc-1")],
        kb_chunk_total=12,
    )

    data = await console_kb_service.list_documents_page(
        object(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        kb_id=KB_ID,
    )

    assert data["kb"].name == "一次拿全"
    assert data["kb"].chunk_count == 12
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
async def test_the_summary_carries_the_stale_document_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「N 篇待更新」原样搬到摘要上（现场九·第三步）。

    它是库级唯一还活着的「要重建」信号：库级生效版本的参数快照随版本层删除，按篇比设置
    指纹就是唯一口径。搬错单位（片 vs 篇）不会报错，只会让界面说「3 篇」而实际是 3 片。
    """

    _install(monkeypatch, kb=_kb(), stale_documents=7)

    data = await console_kb_service.list_summaries(object(), tenant_id=TENANT)  # type: ignore[arg-type]

    assert data["items"][0].stale_documents == 7
