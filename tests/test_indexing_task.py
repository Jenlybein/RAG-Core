"""T4 · Celery 索引任务：离线验收（无 Broker、无网络、无 PostgreSQL）。

三个接缝：
  * ``IndexingService.index_existing_document`` —— Worker 流水线：CAS 抢占后立刻
    提交（两段事务），purge → 写入 → SUCCESS 在同一段事务里；切块器 / embedding /
    存储全部可注入，测试不碰网络。
  * ``app.tasks.indexing._run_indexing`` —— 任务体：重试白名单、失败留痕、
    request_id 关联与两条固定日志名。
  * ``app.tasks.indexing._is_retryable`` —— 可重试判定的真值表。

``self`` 用轻量替身（``request.retries`` / ``max_retries`` / ``retry()`` 抛异常），
不启动 Celery 的 broker 与 Worker 主循环。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.contextvars import clear_contextvars, get_contextvars
from structlog.testing import capture_logs

from app.celery_app import celery_app
from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.providers.parsers.base import ParsedDocument
from app.repositories import document_repository
from app.services import indexing_service
from app.services.indexing_service import INDEXING_FAILED_MESSAGE, IndexingService
from app.tasks import indexing as tasks
from app.tasks.indexing import _is_retryable, _run_indexing
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece

DOC_ID = "doc-1"
TENANT_ID = "tenant_demo"
KB_ID = "kb-1"
PARSE_FAILURE_TEXT = "SECRET-EXCEPTION-TEXT"
SKIPPED_MESSAGE = "indexing skipped, another worker owns it"
INDEXED_MESSAGE = "document indexed"
FAILED_MESSAGE = "document indexing failed"


# --------------------------------------------------------------------------- 替身


class _FakeSession:
    """最小会话替身：只记录 commit / rollback / close，不碰数据库。"""

    def __init__(self, calls: list[str] | None = None) -> None:
        self.info: dict[str, Any] = {}
        self.calls = calls if calls is not None else []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        self.commits += 1
        self.calls.append("commit")

    def rollback(self) -> None:
        self.rollbacks += 1
        self.calls.append("rollback")

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingStore:
    """记录 purge / add 的向量存储替身；名字进调用顺序，便于断言先删后写。"""

    def __init__(self, calls: list[str], name: str = "main") -> None:
        self.calls = calls
        self.name = name
        self.deleted: list[str] = []
        self.tenant_ids: list[str] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(self, _db: Any, document_id: str, *, tenant_id: str) -> None:
        self.calls.append(f"purge:{self.name}")
        self.deleted.append(document_id)
        self.tenant_ids.append(tenant_id)

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.calls.append(f"add:{self.name}")
        self.written.append(chunks)

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("索引链路不应调用检索")


class _RecordingEmbeddings:
    """记录收到的文本与返回的向量。"""

    def __init__(self, calls: list[str], vectors: list[list[float]]) -> None:
        self.calls = calls
        self.vectors = vectors
        self.texts: list[str] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append("embed")
        self.texts.extend(texts)
        return self.vectors

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _StubSplitter:
    def __init__(self, pieces: list[SplitPiece]) -> None:
        self.pieces = pieces

    def split(self, _text: str) -> list[SplitPiece]:
        return self.pieces


def _document() -> SimpleNamespace:
    return SimpleNamespace(
        id=DOC_ID,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title="Demo",
        doc_version=1,
        status=DocumentStatus.PROCESSING,
        source_type="markdown",
        content="",
        source_file_path=None,
        source_filename=None,
        error_message=None,
        index_claimed_at=None,
    )


def _patch_service_environment(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    pieces: list[SplitPiece],
    vectors: list[list[float]],
    claimed: bool = True,
    document: Any | None = None,
    stores: list[_RecordingStore] | None = None,
) -> SimpleNamespace:
    """把仓储、解析入口与三个可注入依赖换成离线替身，返回被测装配。"""

    doc = document if document is not None else _document()
    if stores is None:
        stores = [_RecordingStore(calls)]
    embeddings = _RecordingEmbeddings(calls, vectors)
    claim_calls: list[dict[str, Any]] = []

    def _get_by_id(_db: Any, _document_id: str) -> Any:
        calls.append("get_by_id")
        return doc

    def _try_claim(_db: Any, _document_id: str, **kwargs: Any) -> bool:
        calls.append("claim")
        claim_calls.append(kwargs)
        return claimed

    def _update_status(
        _db: Any, target: Any, status: int, error_message: str | None = None
    ) -> None:
        calls.append(f"update_status:{status}")
        target.status = status
        target.error_message = error_message

    def _prepare(target: Any, *, reparse: bool = False) -> ParsedDocument:
        calls.append("parse")
        return ParsedDocument(content=target.content or "raw text", source_type="markdown")

    monkeypatch.setattr(document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing", _try_claim)
    monkeypatch.setattr(document_repository, "update_status", _update_status)
    monkeypatch.setattr(indexing_service, "prepare_document_content", _prepare)

    service = IndexingService(splitter=_StubSplitter(pieces), embeddings=embeddings, stores=stores)
    return SimpleNamespace(
        service=service,
        session=_FakeSession(calls),
        document=doc,
        stores=stores,
        embeddings=embeddings,
        claim_calls=claim_calls,
    )


# --------------------------------------------------------- IndexingService 流水线


def test_service_indexes_document_through_claim_purge_write_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抢占提交 → 解析 / 向量化 → 先删后写 → SUCCESS，全部按契约顺序发生。"""

    calls: list[str] = []
    pieces = [
        SplitPiece(text="first piece", metadata={"heading_path": "Intro", "section_id": "s1"}),
        SplitPiece(text="second piece", metadata={"heading_path": "Intro", "section_id": "s1"}),
    ]
    env = _patch_service_environment(
        monkeypatch, calls, pieces=pieces, vectors=[[0.1, 0.2], [0.3, 0.4]]
    )

    with capture_logs() as logs:
        count = asyncio.run(env.service.index_existing_document(env.session, DOC_ID))

    assert count == 2
    assert calls == [
        "get_by_id",
        "claim",
        "commit",  # Tx1：抢占后立刻提交，不把 embedding 包在行锁里
        "parse",
        "embed",
        "purge:main",
        "add:main",
        "update_status:1",  # SUCCESS
        "commit",  # Tx2：purge + 写入 + 状态一次性提交
    ]
    assert env.session.commits == 2, "抢占一段、写入一段，恰好两次提交"
    assert env.session.rollbacks == 0
    assert env.claim_calls == [{"tenant_id": TENANT_ID, "allow_success": False}], (
        "Worker 是唯一抢租约的一方，且迟到重复投递不得重新抢占 SUCCESS"
    )
    assert env.document.status == DocumentStatus.SUCCESS

    written = env.stores[0].written[0]
    assert [chunk["meta"]["index"] for chunk in written] == [0, 1]
    assert all(chunk["meta"]["title"] == "Demo" for chunk in written)
    assert all(chunk["meta"]["source_type"] == "markdown" for chunk in written)
    assert written[0]["meta"]["heading_path"] == "Intro", "切块器的 provenance 必须保留"
    assert written[0]["content"] == "first piece"
    assert written[0]["embedding"] == [0.1, 0.2]
    assert written[0]["document_id"] == DOC_ID
    assert written[0]["tenant_id"] == TENANT_ID
    assert written[0]["kb_id"] == KB_ID
    assert written[0]["id"]

    indexed = [entry for entry in logs if entry.get("event") == INDEXED_MESSAGE]
    assert len(indexed) == 1
    assert indexed[0]["document_id"] == DOC_ID
    assert indexed[0]["tenant_id"] == TENANT_ID
    assert indexed[0]["kb_id"] == KB_ID
    assert indexed[0]["chunk_count"] == 2


def test_service_purges_every_store_before_writing_any(monkeypatch: pytest.MonkeyPatch) -> None:
    """purge 遍历存储列表：两个存储都先删完，才开始写。"""

    calls: list[str] = []
    pieces = [SplitPiece(text="only piece", metadata={"heading_path": "Intro"})]
    env = _patch_service_environment(
        monkeypatch,
        calls,
        pieces=pieces,
        vectors=[[0.1]],
        stores=[_RecordingStore(calls, "a"), _RecordingStore(calls, "b")],
    )

    asyncio.run(env.service.index_existing_document(env.session, DOC_ID))

    store_calls = [call for call in calls if call.startswith(("purge:", "add:"))]
    assert store_calls == ["purge:a", "purge:b", "add:a", "add:b"]
    assert env.stores[0].deleted == [DOC_ID]
    assert env.stores[1].deleted == [DOC_ID]
    assert env.stores[0].tenant_ids == [TENANT_ID]
    assert env.stores[1].tenant_ids == [TENANT_ID]
    assert env.stores[0].written[0][0]["content"] == "only piece"
    assert env.stores[1].written[0][0]["content"] == "only piece"


def test_service_skips_when_another_worker_owns_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS 抢不到：返回 0、不向量化、不删写，并留下 skip 日志。"""

    calls: list[str] = []
    pieces = [SplitPiece(text="piece", metadata={"heading_path": "Intro"})]
    env = _patch_service_environment(
        monkeypatch, calls, pieces=pieces, vectors=[[0.1]], claimed=False
    )

    with capture_logs() as logs:
        count = asyncio.run(env.service.index_existing_document(env.session, DOC_ID))

    assert count == 0
    assert calls == ["get_by_id", "claim", "commit"]
    assert env.embeddings.texts == [], "抢不到就绝不调用 embedding"
    assert env.stores[0].deleted == []
    assert env.stores[0].written == []

    skipped = [entry for entry in logs if entry.get("event") == SKIPPED_MESSAGE]
    assert len(skipped) == 1
    assert skipped[0]["document_id"] == DOC_ID
    assert skipped[0]["tenant_id"] == TENANT_ID
    assert skipped[0]["kb_id"] == KB_ID


def test_service_rejects_missing_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """文档不存在 → AppError(NOT_FOUND)，既不抢占也不写任何东西。"""

    calls: list[str] = []
    env = _patch_service_environment(
        monkeypatch, calls, pieces=[SplitPiece(text="p")], vectors=[[0.1]], document=None
    )
    monkeypatch.setattr(indexing_service.document_repository, "get_by_id", lambda _db, _id: None)

    with pytest.raises(AppError) as excinfo:
        asyncio.run(env.service.index_existing_document(env.session, DOC_ID))

    assert excinfo.value.code == ErrorCode.NOT_FOUND
    assert calls == []
    assert env.session.commits == 0


def test_service_rejects_empty_pieces_before_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """切块结果为空 → DOCUMENT_CONTENT_EMPTY，且不向量化、不删写。"""

    calls: list[str] = []
    env = _patch_service_environment(monkeypatch, calls, pieces=[], vectors=[])

    with pytest.raises(AppError) as excinfo:
        asyncio.run(env.service.index_existing_document(env.session, DOC_ID))

    assert excinfo.value.code == ErrorCode.DOCUMENT_CONTENT_EMPTY
    assert calls == ["get_by_id", "claim", "commit", "parse"]
    assert env.stores[0].deleted == []


def test_service_rejects_embedding_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量数量与切片数量不一致是硬错误：直接抛 ValueError，不写入半成品。"""

    calls: list[str] = []
    pieces = [
        SplitPiece(text="first piece", metadata={"heading_path": "Intro"}),
        SplitPiece(text="second piece", metadata={"heading_path": "Intro"}),
    ]
    env = _patch_service_environment(monkeypatch, calls, pieces=pieces, vectors=[[0.1]])

    with pytest.raises(ValueError, match="embedding count mismatch"):
        asyncio.run(env.service.index_existing_document(env.session, DOC_ID))

    assert "purge:main" not in calls
    assert "add:main" not in calls
    assert env.session.commits == 1


# ------------------------------------------------------------- 任务体（_run_indexing）


class _RetrySignal(Exception):
    """``self.retry()`` 的替身异常：真实 Celery 的 Retry 同样是异常。"""


class _FakeTask:
    """轻量 ``self``：只提供任务体用到的三个接口。"""

    def __init__(self, *, retries: int = 0, max_retries: int = 2) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.max_retries = max_retries
        self.retry_calls: list[BaseException] = []

    def retry(self, *, exc: BaseException) -> None:
        self.retry_calls.append(exc)
        raise _RetrySignal


class _FailingService:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def index_existing_document(
        self, _db: Any, _document_id: str, *, reparse: bool = False
    ) -> int:
        raise self.exc


def _patch_task_runtime(
    monkeypatch: pytest.MonkeyPatch, *, service: Any, session: _FakeSession
) -> list[dict[str, Any]]:
    """任务体替身装配；返回 ``release_index_claim`` 的调用记录。"""

    monkeypatch.setattr(tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(tasks, "IndexingService", lambda: service)
    releases: list[dict[str, Any]] = []

    def _release(_db: Any, document_id: str, *, tenant_id: str) -> None:
        session.calls.append("release-claim")
        releases.append({"document_id": document_id, "tenant_id": tenant_id})

    monkeypatch.setattr(tasks.document_repository, "release_index_claim", _release)
    return releases


def test_task_retries_whitelisted_error_while_retries_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可重试错误且还有次数 → 调用 self.retry，不写失败状态。"""

    session = _FakeSession()
    failure = AppError(ErrorCode.EMBEDDING_FAILED, "embedding upstream timeout")
    releases = _patch_task_runtime(monkeypatch, service=_FailingService(failure), session=session)
    marked: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks, "_mark_failed", lambda *args, **_kwargs: marked.append(args))
    task = _FakeTask(retries=0, max_retries=2)

    with capture_logs() as logs:
        with pytest.raises(_RetrySignal):
            _run_indexing(
                task,
                DOC_ID,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                request_id="req-1",
                reparse=True,
            )

    assert session.calls == [
        "rollback",
        "release-claim",
        "commit",
    ], "必须先释放租约并提交，再 self.retry；否则重试的 CAS 永远抢不到"
    assert releases == [{"document_id": DOC_ID, "tenant_id": TENANT_ID}]
    assert task.retry_calls == [failure]
    assert marked == [], "可重试时不应写 status=2"
    assert [entry for entry in logs if entry.get("event") == FAILED_MESSAGE] == []
    assert session.closed, "即使抛出 retry，也必须关掉 Worker 自己的会话"
    assert "request_id" not in get_contextvars(), "retry 路径同样要清掉关联标识"


def test_task_marks_failed_for_non_retryable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """不可重试错误：不重试，写 status=2 + 脱敏固定文案，异常原文只进日志。"""

    session = _FakeSession()
    document = _document()
    document.index_claimed_at = now()
    failure = AppError(ErrorCode.DOCUMENT_PARSE_FAILED, PARSE_FAILURE_TEXT)
    _patch_task_runtime(monkeypatch, service=_FailingService(failure), session=session)
    monkeypatch.setattr(document_repository, "get_by_id", lambda _db, _id: document)
    task = _FakeTask(retries=0, max_retries=2)

    with capture_logs() as logs:
        _run_indexing(
            task, DOC_ID, tenant_id=TENANT_ID, kb_id=KB_ID, request_id=None, reparse=False
        )

    assert task.retry_calls == []
    assert session.rollbacks == 1, "先回滚半成品事务"
    assert document.status == DocumentStatus.FAILED
    assert document.error_message == INDEXING_FAILED_MESSAGE, "只写脱敏固定文案"
    assert PARSE_FAILURE_TEXT not in (document.error_message or ""), "异常原文不得落库"
    assert document.index_claimed_at is None, "失败终态也必须清掉租约"
    assert session.commits == 1

    failed = [entry for entry in logs if entry.get("event") == FAILED_MESSAGE]
    assert len(failed) == 1
    assert failed[0]["document_id"] == DOC_ID
    assert failed[0]["tenant_id"] == TENANT_ID
    assert failed[0]["kb_id"] == KB_ID


def test_task_marks_failed_when_retries_are_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """可重试错误但次数已用尽：不再 retry，转为失败留痕。"""

    session = _FakeSession()
    document = _document()
    failure = AppError(ErrorCode.EMBEDDING_FAILED, PARSE_FAILURE_TEXT)
    _patch_task_runtime(monkeypatch, service=_FailingService(failure), session=session)
    monkeypatch.setattr(document_repository, "get_by_id", lambda _db, _id: document)
    task = _FakeTask(retries=2, max_retries=2)

    with capture_logs() as logs:
        _run_indexing(
            task, DOC_ID, tenant_id=TENANT_ID, kb_id=KB_ID, request_id=None, reparse=False
        )

    assert task.retry_calls == []
    assert document.status == DocumentStatus.FAILED
    assert document.error_message == INDEXING_FAILED_MESSAGE
    assert len([entry for entry in logs if entry.get("event") == FAILED_MESSAGE]) == 1


def test_task_binds_request_id_for_the_duration_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_id 在任务运行期间可见（串起上传日志与 Worker 日志），结束后清空。"""

    session = _FakeSession()
    observed: list[dict[str, Any]] = []

    class _RecordingService:
        async def index_existing_document(
            self, _db: Any, _document_id: str, *, reparse: bool = False
        ) -> int:
            observed.append(get_contextvars())
            return 2

    _patch_task_runtime(monkeypatch, service=_RecordingService(), session=session)
    clear_contextvars()

    _run_indexing(
        _FakeTask(), DOC_ID, tenant_id=TENANT_ID, kb_id=KB_ID, request_id="req-1", reparse=False
    )

    assert observed == [{"request_id": "req-1"}]
    assert "request_id" not in get_contextvars()
    assert session.closed


# ------------------------------------------------------------ _is_retryable 真值表


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.API_TIMEOUT,
        ErrorCode.API_REQUEST_ERROR,
        ErrorCode.EMBEDDING_FAILED,
        ErrorCode.SYSTEM_BUSY,
    ],
)
def test_is_retryable_accepts_whitelisted_codes(code: ErrorCode) -> None:
    assert _is_retryable(AppError(code, "transient failure"))


@pytest.mark.parametrize(
    "exc",
    [
        AppError(ErrorCode.DOCUMENT_PARSE_FAILED, "bad document"),
        AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "empty document"),
        AppError(ErrorCode.NOT_FOUND, "missing document"),
        ValueError("embedding count mismatch"),
        RuntimeError("unexpected"),
    ],
)
def test_is_retryable_rejects_everything_else(exc: BaseException) -> None:
    assert not _is_retryable(exc)


# ---------------------------------------------------------- Celery 应用的任务接线


def test_celery_app_includes_task_module() -> None:
    """Worker 从 app.celery_app 启动时必须自带任务模块，否则任务无人认领。

    没有 include，Worker 进程根本不 import ``app.tasks.indexing``，
    ``enqueue_document`` 投出去的任务会被当作 "Received unregistered task" 消费掉，
    整条索引流水线静默不跑。
    """

    assert "app.tasks.indexing" in list(celery_app.conf.include or [])


def test_importing_task_module_registers_task_on_app() -> None:
    """导入任务模块后，任务必须以 Celery 派生的全名注册在 app 上。"""

    key = "app.tasks.indexing.index_document_task"
    assert key in celery_app.tasks, f"registered tasks: {sorted(celery_app.tasks)}"
