"""T7 · 跨层端到端：上传 → 投递 → Worker 解析 / 切块 / 向量化 / 写入 → SUCCESS。

这是唯一一条真正串起**两层**的测试（其余各层都是单点验证）：

* ASGI 边界发一次 multipart ``.md`` 上传（``POST /api/v1/documents/upload``）；
* 把 ``document_service.enqueue_document`` 换成「把任务投进**真的** Worker 常驻循环、
  跑真实流水线」的替身——真实 ``IndexingService.index_existing_document`` + 真实
  ``MarkdownStructuredSplitter`` + 真实 Markdown 解析，只把 embedding 与向量存储
  换成离线替身；
* 断言链条末端：切片 ``meta`` 带着 ``heading_path`` / ``chunk_type`` /
  ``section_id`` / ``title`` / ``source_type``，表格完整留在一片里，文档状态落到
  SUCCESS，且 purge 发生在 add 之前。

为什么投递替身用的是**真的** ``get_worker_loop()``：票 #132 之后生产路径就是「任务体把
工厂函数投进常驻循环」，这里照抄那个形状（而不是另起线程 ``asyncio.run``）——于是
「上传登记 → 投递 → Worker 抢租约 → 写入 → 状态回写」这条链路的**接线**本身也在被断言。

离线：异步内存会话替身（无 PostgreSQL）、知识库仓储和 CAS 抢占换替身、embedding 返回
固定 1536 维向量、向量存储记录调用，不发网络请求、不下载资产。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.base import now
from app.db.worker_loop import get_worker_loop
from app.models.document import Document
from app.models.knowledge_base import FORMAT_MARKDOWN, SPLIT_STRUCTURE
from app.repositories import document_repository
from app.services.indexing_service import IndexingService
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.status import DocumentStatus
from tests.fake_db import ZERO_COUNT

KB_ID = "kb-1"
TENANT_ID = settings.DEV_TENANT_ID
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "markdown" / "structured_notes.md"
SOURCE_FILENAME = "Pipeline Notes.md"
EMBEDDING_DIM = 1536

# 切块参数钉在测试里（不读 .env）：这条测试要验证的是「跨层串起来」，不是调参。
SPLITTER = MarkdownStructuredSplitter(chunk_size=800, chunk_overlap=100, table_max_rows=10)

TABLE_LINES = [
    "| Field | Meaning |",
    "| --- | --- |",
    "| document_id | stable identifier |",
    "| heading_path | section provenance |",
    "| chunk_type | table, list or prose |",
]

REQUIRED_META = {"heading_path", "chunk_type", "section_id", "title", "source_type"}


class _FakeSession:
    """异步内存会话替身：文档行留在内存里，只记录提交 / 回滚次数。

    ``get`` 故意写成**同步**：真 ``AsyncSession.get`` 是异步的，但这一份是替身，而桥
    （``app/db/dual_session``）按「结果可不可 await」判断——两种写法都能被它正确接住。
    断言侧因此可以直接 ``session.get(...)``，不必把用例写成协程。
    """

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self._rows: dict[str, Any] = {}
        self.commits = 0
        self.rollbacks = 0

    def add(self, instance: Any) -> None:
        self._rows[instance.id] = instance

    def get(self, _model: Any, ident: Any) -> Any | None:
        return self._rows.get(ident)

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: Any) -> Any:
        # 上传前的静态配额会数行；这个替身表示空库。
        return ZERO_COUNT

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        return None


class _StubEmbeddings:
    """固定 1536 维向量；记录收到的切片文本，绝不发模型请求。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.vectors: list[list[float]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        self.vectors = [[float(index + 1)] * EMBEDDING_DIM for index in range(len(texts))]
        return self.vectors

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _RecordingStore:
    """记录 purge / add 的向量存储替身；调用顺序即「先删后写」的证据。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.tenant_ids: list[str] = []
        self.written: list[list[dict[str, Any]]] = []

    async def delete_by_document_id(self, _db: Any, document_id: str, *, tenant_id: str) -> None:
        self.calls.append("purge")
        self.deleted.append(document_id)
        self.tenant_ids.append(tenant_id)

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.calls.append("add")
        self.written.append(chunks)

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("索引链路不应调用检索")


@pytest.fixture(autouse=True)
def isolated_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """日志落到 tmp 目录，避免这条端到端测试往仓库的 logs/ 写文件。"""

    import logging

    logging_mod = importlib.import_module("app.core.logging")
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(logging_mod, "_configured", False)
    logging_mod.setup_logging()
    yield
    logging.shutdown()
    logging_mod._configured = False
    logging_mod.setup_logging(log_dir=tmp_path / "logs-restored", log_level="INFO")


@pytest.fixture
def pipeline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """离线上传接缝 + 「会跑真实 Worker 流水线」的投递替身。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    document_service = importlib.import_module("app.services.document_service")
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    session = _FakeSession()
    embeddings = _StubEmbeddings()
    store = _RecordingStore()
    uploads_dir = tmp_path / "uploads"
    claimed: list[str] = []

    monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", str(uploads_dir))

    # 目标库是 structure（白名单只有 markdown）：上传准入会读这份配置，
    # 所以替身必须带齐策略与白名单，不能只给 tenant_id。
    async def _visible_kb(_db: Any, _kb_id: str, *, tenant_id: str) -> Any:
        return SimpleNamespace(
            id=KB_ID,
            tenant_id=tenant_id,
            split_strategy=SPLIT_STRUCTURE,
            allowed_formats=[FORMAT_MARKDOWN],
            settings={},
        )

    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _visible_kb)

    async def _claim(db: Any, document_id: str, **_kwargs: Any) -> bool:
        # 唯一换掉的仓储原语：真实实现是一条 SQL UPDATE（需要 PostgreSQL 行锁），
        # 内存会话跑不了；抢占成功后的两段事务仍由真实 IndexingService 驱动。
        doc = db.get(Document, document_id)
        assert doc is not None
        doc.index_claimed_at = now()
        claimed.append(document_id)
        return True

    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)

    def _consume_index_task(**kwargs: Any) -> None:
        """投递替身：把任务投进**真的** Worker 常驻循环（与生产同形，不另起线程）。"""

        service = IndexingService(splitter=SPLITTER, embeddings=embeddings, stores=[store])
        get_worker_loop().run(
            lambda: service.index_existing_document(session, kwargs["document_id"])
        )

    monkeypatch.setattr(document_service, "enqueue_document", _consume_index_task)

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: session
    main.app.dependency_overrides[get_current_key] = lambda: DevKeyContext(tenant_id=TENANT_ID)
    try:
        with TestClient(main.app) as client:
            yield SimpleNamespace(
                client=client,
                session=session,
                store=store,
                embeddings=embeddings,
                uploads_dir=uploads_dir,
                claimed=claimed,
            )
    finally:
        main.app.dependency_overrides.clear()


def test_markdown_upload_runs_the_whole_pipeline_into_indexed_chunks(
    pipeline_env: SimpleNamespace,
) -> None:
    """一次上传走完 登记 → 投递 → 解析 → 切块 → 向量化 → 先删后写 → SUCCESS。"""

    body = FIXTURE_PATH.read_bytes()
    response = pipeline_env.client.post(
        "/api/v1/documents/upload",
        data={"kb_id": KB_ID},
        files={"file": (SOURCE_FILENAME, body, "text/markdown")},
        headers={"X-Request-ID": "t7-pipeline-req"},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["code"] == 0
    assert payload["msg"] == "success"
    data = payload["data"]
    assert data["status"] == DocumentStatus.PROCESSING, "上传接口仍只回「已登记、处理中」"
    assert data["chunk_count"] == 0
    assert data["error_message"] is None

    document_id = data["document_id"]
    stored_path = pipeline_env.uploads_dir / TENANT_ID / f"{document_id}.md"
    assert stored_path.read_bytes() == body, "上传把原始文件落盘，Worker 之后从文件解析"

    doc = pipeline_env.session.get(Document, document_id)
    assert doc is not None
    assert doc.status == DocumentStatus.SUCCESS, "Worker 跑完后状态必须落到 SUCCESS"
    assert doc.error_message is None
    assert doc.index_claimed_at is not None
    assert doc.source_filename == SOURCE_FILENAME
    # 相对存储根的 POSIX 路径：写绝对路径会让容器里的 Worker 找不到文件。
    assert doc.source_file_path == f"{TENANT_ID}/{document_id}.md"
    assert "\\" not in doc.source_file_path
    assert doc.source_type == "markdown"

    assert pipeline_env.claimed == [document_id], "Worker 必须走一次 CAS 抢占"
    assert pipeline_env.session.rollbacks == 0
    # 上传登记 + 抢占提交 + purge/写入/状态提交：两段事务各一次，再加登记那一次。
    assert pipeline_env.session.commits == 3

    assert pipeline_env.store.calls == ["purge", "add"], "先删旧切片，再写新切片"
    assert pipeline_env.store.deleted == [document_id]
    assert pipeline_env.store.tenant_ids == [TENANT_ID], "purge 也必须强制带租户"
    assert len(pipeline_env.store.written) == 1
    chunks = pipeline_env.store.written[0]

    # ── 切块：真实 Markdown 结构切块器的产物 ──
    # h3 是切片边界、表格带行级片之后，样例文档切出 8 片：h1 引导段、Storage Layout
    # 的正文 + 整表 + 3 个行级片、Chunking Contract 的列表、其下的 Worker Steps。
    assert len(chunks) == 8
    assert [chunk["meta"]["heading_path"] for chunk in chunks] == [
        "Pipeline Notes",
        "Pipeline Notes > Storage Layout",
        "Pipeline Notes > Storage Layout",
        "Pipeline Notes > Storage Layout",
        "Pipeline Notes > Storage Layout",
        "Pipeline Notes > Storage Layout",
        "Pipeline Notes > Chunking Contract",
        "Pipeline Notes > Chunking Contract > Worker Steps",
    ]
    assert [chunk["meta"]["chunk_type"] for chunk in chunks] == [
        "prose",
        "prose",
        "table",
        "table_row",
        "table_row",
        "table_row",
        "list",
        "prose",
    ]
    # 解析器 metadata 必须跟着切片走：否则 OCR 文档的 `metadata.engine` 无从查起
    # （真机验收时踩过——切片里只有切块字段，engine/ocr_pages 全丢了）。
    assert {chunk["meta"]["parser"] for chunk in chunks} == {"markdown"}
    assert [chunk["meta"]["index"] for chunk in chunks] == [0, 1, 2, 3, 4, 5, 6, 7]

    for chunk in chunks:
        assert REQUIRED_META <= set(chunk["meta"]), "切块 provenance 必须一路带进切片 meta"
        assert chunk["meta"]["title"] == "Pipeline Notes", "标题来自上传时的文件名主干"
        assert chunk["meta"]["source_type"] == "markdown"
        assert chunk["document_id"] == document_id
        assert chunk["tenant_id"] == TENANT_ID
        assert chunk["kb_id"] == KB_ID
        assert len(chunk["embedding"]) == EMBEDDING_DIM
        section_id = chunk["meta"]["section_id"]
        assert isinstance(section_id, str) and len(section_id) == 64

    # 同一章节的 5 片（正文 + 整表 + 3 行级）共享 section_id；子章节的 parent 指回父章节。
    assert {chunk["meta"]["section_id"] for chunk in chunks[1:6]} == {
        chunks[1]["meta"]["section_id"]
    }
    assert chunks[6]["meta"]["section_id"] != chunks[7]["meta"]["section_id"]
    assert chunks[1]["meta"]["parent_section_id"] == chunks[0]["meta"]["section_id"]
    assert chunks[7]["meta"]["parent_section_id"] == chunks[6]["meta"]["section_id"]

    # ── 表格：整块留在一片里，没有被行数上限或字符窗口切开；行级片另存 ──
    table_chunks = [chunk for chunk in chunks if chunk["meta"]["chunk_type"] == "table"]
    row_chunks = [chunk for chunk in chunks if chunk["meta"]["chunk_type"] == "table_row"]
    assert len(table_chunks) == 1
    assert table_chunks[0]["content"] == "\n".join(TABLE_LINES)
    assert len(row_chunks) == 3, "三个数据行各一片行级片"
    assert row_chunks[0]["content"] == "Field=document_id，Meaning=stable identifier"
    assert [chunk["meta"]["chunk_type"] for chunk in chunks if "|" in chunk["content"]] == ["table"]

    # ── h3 是切片边界：Worker Steps 自成一片，路径精确到它，正文归它而不是父节 ──
    assert chunks[7]["meta"]["heading_path"].endswith("> Worker Steps")
    assert "### Worker Steps" in chunks[7]["content"]
    assert "### Worker Steps" not in chunks[6]["content"]

    # ── 向量化：送进 embedding 的是检索文本（文档名 + 章节路径 + 正文），
    #    不是切片原文；每个切片各一条 1536 维向量，数量与切片一一对应。 ──
    assert len(pipeline_env.embeddings.texts) == len(chunks)
    for text, chunk in zip(pipeline_env.embeddings.texts, chunks):
        assert text == (
            f"文档：Pipeline Notes 章节：{chunk['meta']['heading_path']} 正文：{chunk['content']}"
        ), "向量化输入必须是该切片的检索文本，且以标题与章节路径开头"
    assert pipeline_env.embeddings.texts != [chunk["content"] for chunk in chunks], (
        "检索文本与切片原文必须是两份不同的文本"
    )
    # 再用一条完整字面值钉死格式，避免上面的断言跟着实现一起漂移。
    assert pipeline_env.embeddings.texts[0] == (
        "文档：Pipeline Notes 章节：Pipeline Notes 正文：" + chunks[0]["content"]
    )
    assert len(pipeline_env.embeddings.vectors) == len(chunks)
    assert all(len(vector) == EMBEDDING_DIM for vector in pipeline_env.embeddings.vectors)
    assert chunks[0]["embedding"] == pipeline_env.embeddings.vectors[0]


def test_same_markdown_loses_heading_path_in_a_size_kb(
    pipeline_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一份 markdown、同一套 Worker：structure 库留章节路径，size 库不留。

    这是「策略即准入」的兑现——库级策略不只是个配置项，它真的换了切块器。
    与上一条测试的唯一区别是**不注入切块器**：让 Worker 自己去读库配置。
    """

    from app.models.knowledge_base import SPLIT_SIZE

    document_service = importlib.import_module("app.services.document_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    async def _split_config(_db: Any, _kb_id: str, *, tenant_id: str) -> Any:
        return (SPLIT_SIZE, 800)

    monkeypatch.setattr(kb_repo, "get_split_config", _split_config)

    session = pipeline_env.session
    store = pipeline_env.store
    embeddings = pipeline_env.embeddings

    def _consume(**kwargs: Any) -> None:
        service = IndexingService(splitter=None, embeddings=embeddings, stores=[store])
        get_worker_loop().run(
            lambda: service.index_existing_document(session, kwargs["document_id"])
        )

    monkeypatch.setattr(document_service, "enqueue_document", _consume)

    response = pipeline_env.client.post(
        "/api/v1/documents/upload",
        data={"kb_id": KB_ID},
        files={"file": (SOURCE_FILENAME, FIXTURE_PATH.read_bytes(), "text/markdown")},
        headers={"X-Request-ID": "t7-size-req"},
    )

    assert response.status_code == 200
    chunks = store.written[-1]

    assert all(chunk["meta"].get("heading_path") in (None, "") for chunk in chunks), (
        "size 切块不认标题，不该产出章节路径"
    )
    assert all(chunk["meta"].get("section_id") in (None, "") for chunk in chunks), (
        "没有章节边界就没有 section_id，折叠与页码对这类切片不生效"
    )
    assert len(chunks) != 8, "两种策略切出来的形状必须不同，而不是同一份结果换个名字"
