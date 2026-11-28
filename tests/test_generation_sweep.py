"""代落后扫描（现场九·第七步）：哪些篇会被重投、哪些不许被碰。

## 为什么这些用例值得写

这一步会在**夜里自动重算**别人的库。判据写宽一格，代价就是「某天早上发现全库被重算过」；
写窄一格，症状则完全相反——界面说「12 篇待更新」，而它们永远等不到那一夜。两种错都不会
报错，所以判据必须逐条钉住。

## 判据是三条的并集

1. 从没记过切块参数指纹（`settings_hash IS NULL`）；
2. 记下的指纹与**库当前设置**不一致（改过设置没重建）；
3. 压根没有产物（`current_index_generation IS NULL`）——只比指纹会漏掉这一批：一篇从来没
   建成的 `SUCCESS` 文档，指纹可能恰好等于库当前设置（迁移回填就是按生效版本的参数算的），
   于是它永远不在「待更新」里，也永远不在检索结果里。

只数 `SUCCESS`：正在跑、排队、失败的篇本来就会被别的机制处理（再投一次只会撞状态 CAS）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import generation_sweep
from app.services.index_generation import settings_fingerprint
from tests.support.sqlite_schema import KNOWLEDGE_BASES_DDL

TENANT = "tenant_demo"
KB = "kb-1"

DOCUMENTS_DDL = """
CREATE TABLE documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL DEFAULT 't',
    content TEXT,
    source_type VARCHAR(32) NOT NULL DEFAULT 'markdown',
    source_filename VARCHAR(255),
    status INTEGER NOT NULL DEFAULT 1,
    current_version INTEGER NOT NULL DEFAULT 1,
    current_index_generation INTEGER,
    settings_hash VARCHAR(64),
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

#: 库当前设置（`build_params_of` 的缺省输出）对应的指纹：与迁移回填时的算法同一个函数。
CURRENT = settings_fingerprint(
    {"split_strategy": "structure", "chunk_size": 512, "section_part_max_chars": 2000}
)

#: 「改过设置」之后的指纹：片大小不同。
OTHER = settings_fingerprint(
    {"split_strategy": "structure", "chunk_size": 300, "section_part_max_chars": 2000}
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(text(KNOWLEDGE_BASES_DDL))
        await connection.execute(text(DOCUMENTS_DDL))
        await connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, split_strategy, "
                "created_at, updated_at) VALUES (:id, :tenant, '库', 'structure', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": KB, "tenant": TENANT},
        )
    async with AsyncSession(engine) as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


async def _document(
    db: AsyncSession,
    document_id: str,
    *,
    status: int = 1,
    settings_hash: str | None = CURRENT,
    generation: int | None = 1,
    kb_id: str = KB,
    tenant_id: str = TENANT,
) -> None:
    await db.execute(
        text(
            "INSERT INTO documents (id, tenant_id, kb_id, status, current_version, "
            "current_index_generation, settings_hash) VALUES "
            "(:id, :tenant, :kb, :status, 1, :generation, :hash)"
        ),
        {
            "id": document_id,
            "tenant": tenant_id,
            "kb": kb_id,
            "status": status,
            "generation": generation,
            "hash": settings_hash,
        },
    )


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """把「置排队中」与「投递」两个副作用换成记录器。

    这一层要证明的是**判据与预算**，不是状态机本身（那条走 `prepare_for_indexing` 用例）。
    但「谁被置成排队中」必须真的被记下来：只记「投了什么」会让「投了但状态没变」这种
    半途而废的错漏过去，而它的症状正是 Worker 抢不到、清扫器却说有动作。
    """

    calls: list[dict] = []

    async def _prepare(_db, document_id: str, *, tenant_id: str, allow_success: bool) -> bool:
        calls.append(
            {"kind": "prepare", "document_id": document_id, "allow_success": allow_success}
        )
        return True

    def _enqueue(**kwargs) -> None:
        calls.append({"kind": "enqueue", **kwargs})

    monkeypatch.setattr(
        generation_sweep.document_repository, "prepare_for_indexing_async", _prepare
    )
    monkeypatch.setattr(generation_sweep, "enqueue_document", _enqueue)
    return calls


@pytest.mark.asyncio
async def test_a_document_whose_settings_hash_lags_is_redispatched(
    session: AsyncSession, queued: list[dict]
) -> None:
    """改过设置没重建：这一篇要被重投，而且是**按单篇重建那条路**（置排队中 → 投递）。"""

    await _document(session, "doc-stale", settings_hash=OTHER)
    await session.commit()

    count = await generation_sweep.redispatch_stale_documents(session)

    assert count == 1
    assert [call["kind"] for call in queued] == ["prepare", "enqueue"], (
        "先置「排队中」再投：顺序反了 Worker 会按状态 CAS 抢不到"
    )
    assert queued[0]["allow_success"] is True, "要的正是「已经成功但产物过时」那一批"
    assert queued[1]["document_id"] == "doc-stale"
    assert queued[1]["request_id"] is None, "清扫器不是请求边界，没有 request_id 可传"


@pytest.mark.asyncio
async def test_a_document_without_any_product_is_redispatched(
    session: AsyncSession, queued: list[dict]
) -> None:
    """指纹恰好对得上、但**一片产物都没有**：只比指纹的判据会漏掉它。"""

    await _document(session, "doc-empty", settings_hash=CURRENT, generation=None)
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session) == 1


@pytest.mark.asyncio
async def test_a_document_that_has_never_recorded_a_fingerprint_is_redispatched(
    session: AsyncSession, queued: list[dict]
) -> None:
    await _document(session, "doc-null", settings_hash=None)
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session) == 1


@pytest.mark.asyncio
async def test_an_up_to_date_document_is_left_alone(
    session: AsyncSession, queued: list[dict]
) -> None:
    """指纹一致且有产物：一个动作都不许发（否则每晚都在重算同一个库）。"""

    await _document(session, "doc-fresh", settings_hash=CURRENT, generation=3)
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session) == 0
    assert queued == []


@pytest.mark.asyncio
async def test_only_successful_documents_are_considered(
    session: AsyncSession, queued: list[dict]
) -> None:
    """排队中 / 索引中 / 失败的篇不许被这一步再投一次：它们的处置归别的机制。"""

    for index, status in enumerate((0, 2, 3, 4)):
        await _document(session, f"doc-{status}", status=status, settings_hash=OTHER)
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session) == 0
    assert queued == []


@pytest.mark.asyncio
async def test_documents_of_another_tenant_or_kb_are_not_touched(
    session: AsyncSession, queued: list[dict]
) -> None:
    """指纹按**库**算：别的库/别的租户的篇不许因为「属于某个算过的库」被顺手带上。"""

    await _document(session, "doc-foreign-tenant", settings_hash=OTHER, tenant_id="other-tenant")
    await _document(session, "doc-foreign-kb", settings_hash=OTHER, kb_id="kb-other")
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session) == 0
    assert queued == []


@pytest.mark.asyncio
async def test_the_nightly_budget_caps_the_batch(session: AsyncSession, queued: list[dict]) -> None:
    """一次最多 `limit` 篇：库里真有一千篇待更新时要跑二十晚，而不是一夜把队列堵死。

    留在后面的篇不是「丢了」——下一次运行还在原地等它（判据不消耗状态）。
    """

    for index in range(3):
        await _document(session, f"doc-{index}", settings_hash=OTHER)
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session, limit=2) == 2
    assert [call["document_id"] for call in queued if call["kind"] == "enqueue"] == [
        "doc-0",
        "doc-1",
    ], "按 updated_at 升序取最早的那几篇，顺序稳定"


@pytest.mark.asyncio
async def test_a_document_that_was_just_claimed_is_skipped(
    session: AsyncSession, queued: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """置「排队中」失败 = 刚被别处抢先（人工重建 / 另一个清扫器）：跳过，不许重复投递。"""

    await _document(session, "doc-stale", settings_hash=OTHER)
    await session.commit()

    async def _refuse(_db, _document_id: str, *, tenant_id: str, allow_success: bool) -> bool:
        return False

    monkeypatch.setattr(generation_sweep.document_repository, "prepare_for_indexing_async", _refuse)

    assert await generation_sweep.redispatch_stale_documents(session) == 0
    assert [call["kind"] for call in queued] == [], "抢不到就不投，避免重复消息"


@pytest.mark.asyncio
async def test_a_zero_budget_does_nothing(session: AsyncSession, queued: list[dict]) -> None:
    await _document(session, "doc-stale", settings_hash=OTHER)
    await session.commit()

    assert await generation_sweep.redispatch_stale_documents(session, limit=0) == 0
