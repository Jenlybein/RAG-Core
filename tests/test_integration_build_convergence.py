"""#214 · **真库**上的构建收敛：判据读的是 ``chunks`` 表，结论只发生一次状态迁移。

## 为什么这组用例必须在真库上跑

这一票要堵的缺陷，机制就是「库里的行」与「代码以为的行」不一致：旧实现用一个假计数当判据，
于是「一篇都没真重建」的构建也能 ready 并激活。而离线替身（``tests/fake_db.py``）明确写着
**写语句不模拟行的变化**——它看不见 ``UPDATE`` 的效果，也答不了 ``COUNT(DISTINCT ...)`` 的
真值。在那条缝上写回归用例只会得到假绿，而这个仓库已经因为「断言替换过的函数被调用过」
吃过一次这个亏（父 spec #209 的复盘）。

所以这里的断言只有三种，且都是**外部可观察的事实**：

1. 库里那一行最终是什么状态、``error_message`` 是什么、``documents_done`` 是多少；
2. ``chunks`` 表里到底有没有切片（真值的来源）；
3. 重复收敛之后状态**没有**第二次迁移（幂等）。

## 判据的四种组合在哪验

纯函数那四种组合在 ``tests/test_index_build_convergence.py``（离线，不连库）。这里验的是它
在**真 SQL 上**的读数对不对：

* 「一篇都没真重建」→ ``failed``（绝不是 ``ready``）；
* 「真值够」→ ``ready``，且四项计数照旧要对得上；
* 「把已完成数人为改坏」→ 结论**不变**（判定不依赖计数器）；
* 重复收敛 → 只迁移一次；过了收敛期限的 ``building`` 行能被列出来（给自愈票留的判据）。

## 平台说明

``chunks`` 表在真实部署里是 ``pgvector`` 的 ``VECTOR(1536)``。这组用例只用到它的
``(tenant, kb, document, index_version, doc_version)`` 几列，所以：装了 pgvector 就按模型建表
（``CREATE EXTENSION IF NOT EXISTS vector`` 成功时），没装就按同一批列建一张同形表——CI 的
integration job 用的是官方 ``postgres:16`` 镜像，没有 pgvector，那条路径必须能跑。
Windows 上 psycopg 的异步模式不支持缺省的 ``ProactorEventLoop``（ADR-0023），所以显式换成
``SelectorEventLoop``（与 ``tests/test_integration_cas.py`` 同一个理由）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, TypeVar

import pytest
from sqlalchemy import Engine, create_engine, delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import now
from app.db.transaction import async_commit_now
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
    IndexBuildVersion,
)
from app.models.knowledge_base import KnowledgeBase
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.services.index_build_service import (
    BUILD_NOTHING_TO_REBUILD_MESSAGE,
    IndexBuildNothingToRebuild,
    IndexBuildService,
)
from app.utils.id_generator import new_id
from app.utils.status import DocumentStatus

INTEGRATION_DATABASE_URL = os.environ.get("RAG_INTEGRATION_DATABASE_URL")

TENANT_ID = "tenant_conv_214"
KB_ID = "kb-conv-214"
#: 第二个库：只用来摆「期限还没到的那一行」（同一库同时只能有一条 ``building``）。
OTHER_KB_ID = "kb-conv-214-b"
DOC_A = "doc-conv-214-aaaa"
DOC_B = "doc-conv-214-bbbb"
DOC_FAILED = "doc-conv-214-cccc"

T = TypeVar("T")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_DATABASE_URL,
        reason="RAG_INTEGRATION_DATABASE_URL is not set; integration tests skipped",
    ),
]

#: 没有 pgvector 时 ``chunks`` 的同形建表语句（只保留本组用例会读写的列）。
_CHUNKS_WITHOUT_PGVECTOR = """
CREATE TABLE IF NOT EXISTS chunks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    kb_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,
    metadata JSONB NOT NULL,
    index_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    doc_version INTEGER NOT NULL DEFAULT 1
)
"""


# ────────────────────────────────────────────────────────────── 夹具


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """在真库上建四张表；本文件只删自己种下的行，绝不 DROP 表。"""

    assert INTEGRATION_DATABASE_URL is not None
    engine = create_engine(INTEGRATION_DATABASE_URL)
    _create_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _create_schema(engine: Engine) -> None:
    """建表：三张与矢量无关的走模型，``chunks`` 优先走 pgvector、否则退同形表。

    两段各开一笔事务：``CREATE EXTENSION`` 失败会让那一笔整体中止（Postgres 里一条语句失败
    之后同一事务里什么都执行不了），所以退路必须在新事务里走——否则「没装 pgvector」这条路径
    会在建表那一步以一个看起来毫不相干的错误炸掉。
    """

    tables = [KnowledgeBase.__table__, Document.__table__, IndexBuildVersion.__table__]
    KnowledgeBase.metadata.create_all(bind=engine, tables=tables)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception:  # noqa: BLE001 - 没装 pgvector 的实例（例如 CI 的 postgres:16）
        with engine.begin() as connection:
            connection.execute(text(_CHUNKS_WITHOUT_PGVECTOR))
        return
    Chunk.metadata.create_all(bind=engine, tables=[Chunk.__table__])


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    """同步会话：只负责种数据与清理（断言走异步那一侧，与生产调用形状一致）。"""

    session = Session(bind=db_engine)
    _delete_seeded_rows(session)
    try:
        yield session
    finally:
        _delete_seeded_rows(session)
        session.close()


def _delete_seeded_rows(session: Session) -> None:
    try:
        session.execute(delete(Chunk).where(Chunk.tenant_id == TENANT_ID))
        session.execute(delete(IndexBuildVersion).where(IndexBuildVersion.tenant_id == TENANT_ID))
        session.execute(delete(Document).where(Document.tenant_id == TENANT_ID))
        session.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_((KB_ID, OTHER_KB_ID))))
        session.commit()
    except Exception:
        session.rollback()
        raise


# ────────────────────────────────────────────────────────── 种数据


def _seed_kb(session: Session, *, kb_id: str = KB_ID) -> None:
    session.add(
        KnowledgeBase(
            id=kb_id,
            tenant_id=TENANT_ID,
            name=f"收敛用例库 {kb_id}",
            description="",
            # 策略是**库级必填项**，格式白名单非空：两者在真行上都由建库路径显式写，
            # 这里照写一份（structure ⇒ markdown），否则真库的 NOT NULL 当场挡住。
            split_strategy="structure",
            allowed_formats=["markdown"],
            settings={},
            active_index_version=None,
        )
    )
    session.commit()


def _seed_document(
    session: Session,
    document_id: str,
    *,
    status: int = DocumentStatus.SUCCESS,
    current_version: int = 1,
) -> None:
    session.add(
        Document(
            id=document_id,
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            title=document_id,
            content="正文",
            source_type="text",
            status=status,
            current_version=current_version,
        )
    )
    session.commit()


def _chunk_row(chunk_id: str, *, document_id: str, version: str, draft: int) -> Chunk:
    """一行切片（真 ORM 行）。

    走 ORM 而不是手写 SQL，是为了同一个用例能跑在两种 ``chunks`` 表上：装了 pgvector 时
    ``embedding`` 是 ``VECTOR(1536)``（驱动会把列表编码成向量字面量），没装时是同形表里的
    ``TEXT``（同一份编码原样进文本列）。两条路径都不碰向量本身——真值判据只看
    ``(tenant, kb, document, index_version, doc_version)`` 这几列。
    """

    return Chunk(
        id=chunk_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        document_id=document_id,
        content="正文",
        embedding=[0.0] * 1536,
        meta={},
        index_version=version,
        doc_version=draft,
    )


def _seed_chunk(
    session: Session, chunk_id: str, *, document_id: str, version: str, draft: int
) -> None:
    """直接往 ``chunks`` 里摆一行切片：用它来摆「真值够」与「真值不够」两种现场。"""

    session.add(_chunk_row(chunk_id, document_id=document_id, version=version, draft=draft))
    session.commit()


def _seed_build(
    session: Session,
    *,
    build_id: str,
    version: str,
    kb_id: str = KB_ID,
    status: str = INDEX_BUILD_BUILDING,
    planned: int = 0,
    draft: int = 1,
    done: int = 0,
    deadline: datetime | None = None,
) -> None:
    """手工摆一行构建（给「期限已过」这类只需要一行的用例）。"""

    session.add(
        IndexBuildVersion(
            id=build_id,
            tenant_id=TENANT_ID,
            kb_id=kb_id,
            version=version,
            status=status,
            documents_planned=planned,
            doc_version=draft,
            documents_done=done,
            converge_deadline=deadline if deadline is not None else now() + timedelta(hours=1),
            created_at=now(),
        )
    )
    session.commit()


def _read_build(session: Session, build_id: str) -> IndexBuildVersion:
    """从库里**重新读**一行（不留在会话的对象身份上：要断言的是库里的值）。"""

    session.expire_all()
    row = session.get(IndexBuildVersion, build_id)
    assert row is not None, f"构建行不见了：{build_id}"
    return row


def _count_chunks(session: Session, *, version: str) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM chunks WHERE tenant_id = :tenant AND "
                "kb_id = :kb AND index_version = :version"
            ),
            {"tenant": TENANT_ID, "kb": KB_ID, "version": version},
        ).scalar_one()
    )


def _break_done_counter(session: Session, build_id: str, value: int) -> None:
    """人为把「已完成数」改坏（判定若依赖它，结论就该跟着变——而它**不该**变）。"""

    session.execute(
        text("UPDATE index_build_versions SET documents_done = :done WHERE id = :id"),
        {"done": value, "id": build_id},
    )
    session.commit()


# ─────────────────────────────────────────────────────── 替身（只替外部副作用）


class _NothingRebuilt:
    """流水线替身：每次都「抢不到」（返回 0），一片都不写。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def index_existing_document(
        self,
        _db: Any,
        document_id: str,
        *,
        index_version: str | None = None,
        doc_version: int | None = None,
        allow_success: bool = False,
        reparse: bool = False,
    ) -> int:
        self.calls.append(document_id)
        return 0


class _WritingPipeline:
    """流水线替身：真的往 ``chunks`` 里写片（每篇两片）并提交，返回写入片数。

    它不是「记录器」：写进去的行就是真值的来源，收敛判据读的正是这些行。这里**照真实
    流水线那样在写完那一笔提交**（正文 + 切片 + 状态同一笔），否则同一个会话读得到、别的
    连接读不到，「真值」就变成一件取决于谁在哪个事务里的事——那正是本票要消灭的模糊。
    """

    def __init__(self) -> None:
        self.written: list[str] = []

    async def index_existing_document(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        index_version: str | None = None,
        doc_version: int | None = None,
        allow_success: bool = False,
        reparse: bool = False,
    ) -> int:
        assert index_version is not None
        assert doc_version is not None
        for _index in range(2):
            db.add(
                _chunk_row(
                    new_id(),
                    document_id=document_id,
                    version=index_version,
                    draft=doc_version,
                )
            )
        await async_commit_now(db)
        self.written.append(document_id)
        return 2


class _ChunkCountingVectorStore:
    """向量存储替身：按切片算向量条数（本实现同表，所以两者天然相等）。

    读的是**调用方给的那个会话**：四项计数与真值必须在同一笔视角里看同一批行，否则
    「切片在、向量不在」这种读数会凭空出现（那正是四项计数要挡的那种不一致，不该由替身
    制造出来）。
    """

    async def count_by_scope(
        self, db: AsyncSession, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        result = await db.execute(
            text(
                "SELECT count(*) FROM chunks WHERE tenant_id = :tenant AND "
                "kb_id = :kb AND index_version = :version"
            ),
            {"tenant": tenant_id, "kb": kb_id, "version": index_version},
        )
        return int(result.scalar_one())


def _service(pipeline: Any) -> IndexBuildService:
    """构建服务：只换流水线与向量存储（外部副作用），其余全是真实现。"""

    return IndexBuildService(
        indexing=pipeline,
        vector_store=_ChunkCountingVectorStore(),
        # 关键词能力缺席：这一维不参与四项计数（与 #142 同一口径）。
        keyword_search=NoopKeywordSearchProvider(),
    )


# ───────────────────────────────────────────────────── 异步跑一次场景


def _use_selector_loop_on_windows() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _run(scenario: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """在一个循环里跑完整个场景（异步会话不能跨循环复用）。"""

    url = INTEGRATION_DATABASE_URL
    assert url is not None, "整模块已在没有 RAG_INTEGRATION_DATABASE_URL 时 skip"

    async def _main() -> T:
        engine = create_async_engine(url)
        try:
            async with AsyncSession(bind=engine, expire_on_commit=False) as session:
                return await scenario(session)
        finally:
            await engine.dispose()

    _use_selector_loop_on_windows()
    return asyncio.run(_main())


# ───────────────────────────────────────────── 起构建：快照两列 + 期限


def test_start_build_snapshots_the_participants_the_draft_and_a_deadline(db: Session) -> None:
    """起构建时把「参与篇数」「统一稿号」「收敛期限」写进那一行（真库，读回来验）。"""

    _seed_kb(db)
    _seed_document(db, DOC_A, current_version=1)
    _seed_document(db, DOC_B, current_version=2)
    # 失败文档没有可切的正文，不参与构建，因此也不该进快照的分母。
    _seed_document(db, DOC_FAILED, status=DocumentStatus.FAILED, current_version=2)

    async def _scenario(session: AsyncSession) -> IndexBuildVersion:
        service = _service(_NothingRebuilt())
        return await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)

    row = _run(_scenario)
    stored = _read_build(db, row.id)

    assert stored.status == INDEX_BUILD_BUILDING
    assert stored.documents_planned == 2, "只有 status=1 的文档参与构建（失败的那篇不算）"
    assert stored.doc_version == 2, "统一稿号取参与文档当前稿指针的最大值"
    assert stored.documents_done == 0
    assert stored.converge_deadline > now(), "期限在未来：这一轮还没到必须给结论的时候"


# ─────────────────────────────────────── 一篇都没真重建 → failed，绝不是 ready


def test_a_build_that_rebuilt_nothing_lands_failed_and_never_ready(db: Session) -> None:
    """真值 0、参与篇数 2 → 收敛成 ``failed``；库里一行切片都没有。

    这是 #214 的核心回归：旧实现里这一格能一路 ``ready`` 并激活（四项计数在 0=0=0 时自洽），
    因为唯一的守卫数的是「假计数」。现在判据读真值，0 就是 0。
    """

    _seed_kb(db)
    _seed_document(db, DOC_A)
    _seed_document(db, DOC_B)
    pipeline = _NothingRebuilt()

    async def _scenario(session: AsyncSession) -> str:
        service = _service(pipeline)
        row = await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)
        with pytest.raises(IndexBuildNothingToRebuild):
            await service.build(session, row.id, tenant_id=TENANT_ID)
        return row.id

    build_id = _run(_scenario)
    stored = _read_build(db, build_id)

    assert stored.status == INDEX_BUILD_FAILED, "真值为 0 绝不许 ready"
    assert stored.status != INDEX_BUILD_READY
    assert stored.error_message == BUILD_NOTHING_TO_REBUILD_MESSAGE
    assert stored.documents_done == 0, "抢不到的那两篇不许计入已重建"
    assert stored.chunk_count is None or stored.chunk_count == 0
    assert _count_chunks(db, version=stored.version) == 0
    assert sorted(pipeline.calls) == [DOC_A, DOC_B], "两篇都尝试过，只是都没抢到"


# ─────────────────────────────────────── 真值够 → ready（四项计数照旧要对上）


def test_a_build_that_covered_every_document_goes_ready(db: Session) -> None:
    """每篇都真的写了切片（真值 = 参与篇数）→ ``ready``，四项计数照样要对得上。"""

    _seed_kb(db)
    _seed_document(db, DOC_A)
    _seed_document(db, DOC_B)
    pipeline = _WritingPipeline()

    async def _scenario(session: AsyncSession) -> str:
        service = _service(pipeline)
        row = await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)
        converged = await service.build(session, row.id, tenant_id=TENANT_ID)
        return converged.id

    build_id = _run(_scenario)
    stored = _read_build(db, build_id)

    assert stored.status == INDEX_BUILD_READY
    assert stored.error_message is None
    assert stored.document_count == 2, "ready 时的篇数快照 = 收尾时读到的真值"
    assert stored.documents_done == 2, "进度 = 真的写进切片的篇数"
    assert _count_chunks(db, version=stored.version) == 4, "每篇两片"
    assert stored.chunk_count == 4, "四项计数通过之后落下的切片数"


# ─────────────────────────────────── 把「已完成数」改坏：结论不该跟着变


def test_truth_enough_stays_ready_even_when_the_done_counter_is_broken(db: Session) -> None:
    """真值够、但「已完成数」被人为改成 0 → 仍然 ``ready``（判定不读计数器）。"""

    _seed_kb(db)
    _seed_document(db, DOC_A)
    _seed_document(db, DOC_B)

    async def _scenario(session: AsyncSession) -> str:
        service = _service(_NothingRebuilt())
        row = await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)
        await session.commit()
        return row.id

    build_id = _run(_scenario)
    version = _read_build(db, build_id).version
    # 真值：两篇文档都有切片。计数器：故意写成 0（说不通的值）。
    _seed_chunk(db, "c-doc-a", document_id=DOC_A, version=version, draft=1)
    _seed_chunk(db, "c-doc-b", document_id=DOC_B, version=version, draft=1)
    _break_done_counter(db, build_id, 0)

    async def _converge(session: AsyncSession) -> str:
        service = _service(_NothingRebuilt())
        converged = await service.converge(session, build_id, tenant_id=TENANT_ID)
        return converged.status

    assert _run(_converge) == INDEX_BUILD_READY
    stored = _read_build(db, build_id)
    assert stored.status == INDEX_BUILD_READY, "计数器是 0，真值够 → 照样 ready"


def test_truth_short_stays_failed_even_when_the_done_counter_claims_success(db: Session) -> None:
    """真值不够、但「已完成数」被改成 999 → 仍然 ``failed``（计数器漂高不再能骗过判据）。

    这一格正是旧缺陷的形状：进度显示「全都建好了」，而库里一片都没有。
    """

    _seed_kb(db)
    _seed_document(db, DOC_A)
    _seed_document(db, DOC_B)

    async def _scenario(session: AsyncSession) -> str:
        service = _service(_NothingRebuilt())
        row = await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)
        await session.commit()
        return row.id

    build_id = _run(_scenario)
    _break_done_counter(db, build_id, 999)

    async def _converge(session: AsyncSession) -> str:
        service = _service(_NothingRebuilt())
        converged = await service.converge(session, build_id, tenant_id=TENANT_ID)
        return converged.status

    assert _run(_converge) == INDEX_BUILD_FAILED
    stored = _read_build(db, build_id)
    assert stored.status == INDEX_BUILD_FAILED
    assert stored.error_message == BUILD_NOTHING_TO_REBUILD_MESSAGE


# ───────────────────────────────────────────────── 幂等：只发生一次状态迁移


def test_converging_again_does_not_move_a_settled_build_again(db: Session) -> None:
    """重复收敛只发生一次迁移：``ready`` 之后再收敛（哪怕切片已被清空）仍是 ``ready``。

    幂等是「谁看见谁收尾」的全部依据（最后一篇、清扫器、运维脚本都可能触发）。这里顺手把
    切片删掉，让第二次收敛看到「真值 0」——若判据被重跑，它会把这一行改判 ``failed``，
    而那正是**不该**发生的事：一次构建的结论只有一个，重新裁决不在收敛的职责里。
    """

    _seed_kb(db)
    _seed_document(db, DOC_A)
    _seed_document(db, DOC_B)

    async def _build_and_converge_twice(session: AsyncSession) -> tuple[str, str, str]:
        service = _service(_WritingPipeline())
        row = await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)
        await service.build(session, row.id, tenant_id=TENANT_ID)
        first = (await service.converge(session, row.id, tenant_id=TENANT_ID)).status
        second = (await service.converge(session, row.id, tenant_id=TENANT_ID)).status
        return row.id, first, second

    build_id, first, second = _run(_build_and_converge_twice)
    assert (first, second) == (INDEX_BUILD_READY, INDEX_BUILD_READY)
    stored = _read_build(db, build_id)
    assert stored.status == INDEX_BUILD_READY
    assert stored.error_message is None

    # 清掉切片（模拟「有人在 ready 之后清了这一版」），再收敛一次：状态不该被改判。
    db.execute(delete(Chunk).where(Chunk.tenant_id == TENANT_ID))
    db.commit()

    async def _converge_again(session: AsyncSession) -> str:
        service = _service(_NothingRebuilt())
        return (await service.converge(session, build_id, tenant_id=TENANT_ID)).status

    assert _run(_converge_again) == INDEX_BUILD_READY
    assert _read_build(db, build_id).status == INDEX_BUILD_READY


def test_converging_an_already_failed_build_is_a_quiet_no_op(db: Session) -> None:
    """``failed`` 是终态：再一次收敛什么都不改，也不抛异常（脚本重复跑是常态）。"""

    _seed_kb(db)
    _seed_document(db, DOC_A)

    async def _converge_twice(session: AsyncSession) -> tuple[str, str]:
        service = _service(_NothingRebuilt())
        row = await service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID)
        first = (await service.converge(session, row.id, tenant_id=TENANT_ID)).status
        second = (await service.converge(session, row.id, tenant_id=TENANT_ID)).status
        return first, second

    first, second = _run(_converge_twice)
    assert (first, second) == (INDEX_BUILD_FAILED, INDEX_BUILD_FAILED)


# ─────────────────────────────── 过了收敛期限仍是 building：自愈票的判据


def test_builds_past_their_convergence_deadline_are_listed(db: Session) -> None:
    """期限已过的 ``building`` 行能被列出来；没过的、以及已经收尾的都不算。"""

    _seed_kb(db)
    _seed_kb(db, kb_id=OTHER_KB_ID)
    past = now() - timedelta(minutes=5)
    future = now() + timedelta(hours=2)
    _seed_build(db, build_id="build-overdue", version="v1", deadline=past)
    # 同一库同时只允许一条 building（部分唯一索引），所以「还没到期」的那一行放另一个库。
    _seed_build(db, build_id="build-waiting", version="v1", kb_id=OTHER_KB_ID, deadline=future)

    async def _scenario(session: AsyncSession) -> list[str]:
        service = IndexBuildService(indexing=_NothingRebuilt())
        rows = await service.list_overdue_builds(session, tenant_id=TENANT_ID, at=now())
        return [row.id for row in rows]

    assert _run(_scenario) == ["build-overdue"]

    # 已经收尾的行不该出现在名单里：它不再欠一个结论。
    db.execute(
        text("UPDATE index_build_versions SET status = :status WHERE id = 'build-overdue'"),
        {"status": INDEX_BUILD_ACTIVE},
    )
    db.commit()

    async def _after_settling(session: AsyncSession) -> list[str]:
        service = IndexBuildService(indexing=_NothingRebuilt())
        rows = await service.list_overdue_builds(session, tenant_id=TENANT_ID, at=now())
        return [row.id for row in rows]

    assert _run(_after_settling) == []
