"""旧代回收（现场九·第六步）：每篇留最近 2 代，判据是**集合式**的。

## 为什么这个判据非这么写不可

「保留最近 2 代」看着像个减法：``generation >= 当前代 - 1``。它在代里有空洞时会删错：

某篇的产代依次是 0 / 1 / 5（中间两次失败、或者跳过没跑）。减法的阈值是 ``>= 4``，
于是第 1 代也被删掉——而它恰恰是「上一代」，单篇回滚要用它；与此同时它还会留下第 0 代
吗？不会，也一起删。也就是说：**回滚落点没了，而真正该删的 0 反倒可能因为别的窗口算错而
留下**。

集合式口径问的是「该篇**存在的**最高 N 个代是哪些」，答案与空洞无关：{5, 1} 留下、0 删掉。

## 另一条边界：按篇算，不按库算

代是篇级的属性。同一个库里一篇刚重建到第 9 代、另一篇还停在第 1 代，窗口必须各算各的；
按库取最大值会把慢那篇唯一的一份产物删掉——而它还在被检索读。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import retention

TENANT = "tenant_demo"
KB = "kb-1"

CHUNKS_DDL = """
CREATE TABLE chunks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata JSON NOT NULL,
    doc_version INTEGER NOT NULL DEFAULT '1',
    generation INTEGER NOT NULL DEFAULT 1
)
"""


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.execute(text(CHUNKS_DDL))
    async with AsyncSession(engine) as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


async def _chunk(db: AsyncSession, chunk_id: str, document_id: str, generation: int) -> None:
    await db.execute(
        text(
            "INSERT INTO chunks (id, tenant_id, kb_id, document_id, content, embedding, "
            "metadata, generation) VALUES (:id, :tenant, :kb, :doc, '正文', '[]', '{}', :gen)"
        ),
        {"id": chunk_id, "tenant": TENANT, "kb": KB, "doc": document_id, "gen": generation},
    )


async def _generations_left(db: AsyncSession, document_id: str) -> list[int]:
    rows = (
        await db.execute(
            text("SELECT generation FROM chunks WHERE document_id = :doc ORDER BY generation DESC"),
            {"doc": document_id},
        )
    ).all()
    return [row[0] for row in rows]


@pytest.mark.asyncio
async def test_the_two_newest_generations_survive(session: AsyncSession) -> None:
    for index in range(4):
        await _chunk(session, f"c{index}", "doc-1", index)
    await session.commit()

    outcome = await retention.purge_generations(session)
    await session.commit()

    assert await _generations_left(session, "doc-1") == [3, 2]
    # 读数：篇数与片数各归各的量纲（清扫器只报后者，名字里写明是「片」）。
    assert (outcome.documents, outcome.purged_chunks) == (1, 2)


@pytest.mark.asyncio
async def test_a_gap_in_the_generations_does_not_shift_the_window(session: AsyncSession) -> None:
    """代 0 / 1 / 5：留 {5, 1}，删 0。

    算术减法（``>= 5 - 2 + 1``）会把第 1 代也删掉——而那正是「上一代」，回滚要用。
    """

    for generation in (0, 1, 5):
        await _chunk(session, f"c{generation}", "doc-1", generation)
    await session.commit()

    await retention.purge_generations(session)
    await session.commit()

    assert await _generations_left(session, "doc-1") == [5, 1]


@pytest.mark.asyncio
async def test_the_window_is_per_document_not_per_kb(session: AsyncSession) -> None:
    """同一库里一篇在第 9 代、另一篇只有第 1 代：慢那篇的唯一产物不许被删。"""

    for generation in (8, 9):
        await _chunk(session, f"fast-{generation}", "doc-fast", generation)
    await _chunk(session, "slow-1", "doc-slow", 1)
    await session.commit()

    await retention.purge_generations(session)
    await session.commit()

    assert await _generations_left(session, "doc-fast") == [9, 8]
    assert await _generations_left(session, "doc-slow") == [1], "按库取最大值会把它删掉"


@pytest.mark.asyncio
async def test_a_document_within_the_window_is_untouched(session: AsyncSession) -> None:
    await _chunk(session, "c1", "doc-1", 1)
    await _chunk(session, "c2", "doc-1", 2)
    await session.commit()

    outcome = await retention.purge_generations(session)

    assert outcome.purged_chunks == 0
    assert await _generations_left(session, "doc-1") == [2, 1]


@pytest.mark.asyncio
async def test_a_zero_window_is_refused(session: AsyncSession) -> None:
    """``keep=0`` 会把当前代也删掉——那会让整库检索返回空，且行已经没了。"""

    with pytest.raises(ValueError):
        await retention.purge_generations(session, keep=0)


@pytest.mark.asyncio
async def test_a_generation_spread_over_several_rows_is_ranked_once(
    session: AsyncSession,
) -> None:
    """同一代有好几片时，这几片**共用一个名次**（排名对象是代，不是行）。

    这条防的是 ``row_number`` 的写法：那样第 2 代的 5 片会各自占一个名额，把第 1 代挤出去，
    于是「留 2 代」实际只留了 1 代——回滚落点被删掉，而且不报错。

    用的是**同一稿**的多片：代排名根本不看稿轴（稿由 ``purge_versions`` 管），所以拿两稿
    去构造反而会把「同代不拆」这件事说糊。
    """

    for suffix in ("a", "b"):
        await _chunk(session, f"{suffix}-1", "doc-1", 1)
        await _chunk(session, f"{suffix}-2", "doc-1", 2)
        await _chunk(session, f"{suffix}-3", "doc-1", 3)
    await session.commit()

    await retention.purge_generations(session)
    await session.commit()

    assert await _generations_left(session, "doc-1") == [3, 3, 2, 2], (
        "第 1 代的两片一起删掉；第 2、3 代各两片原样留着"
    )


@pytest.mark.asyncio
async def test_an_empty_chunks_table_is_a_no_op(session: AsyncSession) -> None:
    """空库：读数全 0，而且**不发第二条语句**（稳态成本只有那一条候选查询）。

    稳态是常态——大多数夜里没有一篇超窗。这条把「稳态不碰 chunks 的正文与向量列」钉住，
    否则每晚两次全表扫会变成没人注意到的固定开销。
    """

    outcome = await retention.purge_generations(session)

    assert (outcome.documents, outcome.purged_chunks) == (0, 0)


@pytest.mark.asyncio
async def test_the_batch_cap_bounds_one_night(session: AsyncSession) -> None:
    """``limit`` 封顶一次回收的**篇数**：首次上线的历史库不会变成一次无界删除。

    一撞语句超时就是整批回滚（见函数 docstring 的「失败是整批的」），没有封顶就永远删不完。
    """

    for document_id in ("doc-1", "doc-2"):
        for generation in range(4):
            await _chunk(session, f"{document_id}-g{generation}", document_id, generation)
    await session.commit()

    outcome = await retention.purge_generations(session, limit=1)

    assert outcome.documents == 1, "只处理一批"
    left = [await _generations_left(session, "doc-1"), await _generations_left(session, "doc-2")]
    assert sorted(len(generations) for generations in left) == [2, 4], (
        "一篇被回收，另一篇原样——下一次再扫它"
    )
