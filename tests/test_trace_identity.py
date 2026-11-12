"""#101 · 一次检索可回指：``trace_id`` 与归属索引。

四个接缝，都是**外部可观察**的：

* 纯函数 ``trace_id_for``：同一个 ``log_id`` 恒等、32 位十六进制；
* ASGI ``POST /api/v1/rag/retrieve``：响应 ``metadata.trace_id`` 永远非空，且 ``metadata``
  只多出这一个键；
* 进程级会话工厂（``app.db.session_factory``，旁路写库的**唯一**接缝）：写入成功时落下
  一行、写入失败时只留一条 ``warning``；
* 表 ``retrieval_traces`` 本身：逐列扫描，问句原文一个字都不许在里面。

数据库一律是替身（``tests/fake_db.RecordingSession`` + 只回答 count 的假请求会话），
Provider 一律是离线替身：整套测试不连 PostgreSQL、不连 ES、不发网络。

``trace_id`` 的**逐字节不变**那一半由 ``tests/test_multi_query_union.py`` 的黄金哈希守着
（它在比对前把 ``trace_id`` 归一化成一个占位符）；本文件守的是「这个字段确实存在、
确实非空、确实与库里的那一行对得上」。
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base
from app.models.retrieval_trace import RetrievalTrace
from app.services.trace_identity import delivered_chunk_ids, trace_id_for
from tests.fake_db import AsyncRecordingSession, AsyncSessionAdapter, async_visible_kb
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_trace"
KB_ID = "kb-trace"

#: 问句哨兵：它走完整条检索链路，并回到响应体里；任何一列都不许出现它。
QUERY_SENTINEL = "T101-QUERY-SENTINEL-4f7c"
CONTENT_SENTINEL = "T101-CONTENT-SENTINEL-9a1e"

#: 归属索引只允许这十列（顺序由模型定义，集合不许多也不许少）。
EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "tenant_id",
        "trace_id",
        "kb_ids",
        "chunk_ids",
        "top_k",
        "latency_ms",
        "degraded_reason",
        "empty_reason",
        "created_at",
    }
)

#: 这些**概念**不许以列名形态出现在索引里（原文、改写句、检索句、切片正文、用户与档位）。
FORBIDDEN_COLUMN_WORDS = (
    "query",
    "effective",
    "search",
    "retrieved",
    "content",
    "user_id",
    "profile",
)

#: 票 #77 起质量参数由密钥配置提供；请求助手把旧写法翻译成「造一个 custom 档的假身份」。
IDENTITY = RetrievalIdentity(TENANT_ID)

REPO_ROOT = Path(__file__).resolve().parent.parent
#: 唯一允许提到旧表名的两处：initial 迁移（历史不改写）与本票的改名迁移（它必须说出旧名）。
_OLD_NAME_IN_HISTORY = (Path("migrations") / "versions" / "d657b09304ca_initial.py",)
_OLD_NAME_RE = re.compile(r"retrieval_logs")


# ────────────────────────────────────────────────────────────────── 离线替身


class _FakeResult:
    """``SELECT count(*)`` 的替身：一个数字就是全部（空结果原因要问它）。"""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def all(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:
        return iter(())


class _FakeRequestDb:
    """请求会话替身：只回答「这些库里有多少条可检索切片」，别的读路径一律空。"""

    def __init__(self, *, indexed_chunks: int = 1) -> None:
        self._indexed_chunks = indexed_chunks

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(self._indexed_chunks)

    def close(self) -> None:
        pass


class _FakeEmbedding:
    """离线 embedding 替身，顺便钉住「哨兵真的走到了链路里」。"""

    async def embed_query(self, query: str) -> list[float]:
        assert query == QUERY_SENTINEL, "哨兵必须真的进入检索链路，否则下面的断言是空断言"
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """离线向量库替身：返回预置的几行（不查元数据、不碰数据库）。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)

    async def similarity_search_scope(
        self, db, query_vector, *, tenant_id, index_versions, top_k
    ) -> list[dict]:
        (kb_id,) = index_versions
        return [{**row, "kb_id": kb_id} for row in self._rows[:top_k]]

    async def similarity_search(
        self, db, query_vector, *, tenant_id, kb_id, top_k
    ):  # pragma: no cover
        raise AssertionError("检索走范围版，不许按库循环")


def _rows() -> list[dict]:
    return [
        {
            "document_id": "doc-trace",
            "chunk_id": "chunk-a",
            "title": "部署实施指南",
            "content": CONTENT_SENTINEL,
            "score": 0.9,
        },
        {
            "document_id": "doc-trace",
            "chunk_id": "chunk-b",
            "title": "部署实施指南",
            "content": CONTENT_SENTINEL,
            "score": 0.8,
        },
    ]


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict] | None = None,
    trace_session: AsyncRecordingSession | None = None,
    session_factory: Any = None,
    indexed_chunks: int = 1,
) -> Iterator[dict[str, Any]]:
    """离线 ASGI 客户端；旁路写库的会话由 ``session_factory`` 控制。

    ``trace_session`` 是**独立短事务**那条路上的会话（缺省是一个记录用的替身），
    与 FastAPI 依赖注入的请求会话刻意分开——这两条路必须是两个会话，否则「写入走独立
    短事务」这件事无从证明。
    """

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    factory_mod = importlib.import_module("app.db.session_factory")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    trace_session = trace_session if trace_session is not None else AsyncRecordingSession()
    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(
        service, "get_vector_store", lambda: _FakeVectorStore(_rows() if rows is None else rows)
    )
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)
    # 归属索引写入在异步栈上（第 11 步票 #127）：注入点因此是**异步**会话工厂。
    factory_mod.set_async_session_factory(session_factory or (lambda: trace_session))

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: (
        AsyncSessionAdapter(_FakeRequestDb(indexed_chunks=indexed_chunks))
    )
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {"client": client, "trace_session": trace_session}
    finally:
        main.app.dependency_overrides.clear()
        factory_mod.set_async_session_factory(None)


def _retrieve(client: TestClient, *, mode: str = "vector") -> dict:
    """缺省走 ``vector`` 单模式、不折叠：结果是两片，与写入行的字段一一对应。"""

    IDENTITY.use(mode=mode, top_k=2, include_parent=False)
    response = client.post(RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": QUERY_SENTINEL})
    assert response.status_code == 200
    return response.json()


def _trace_rows(session: AsyncRecordingSession) -> list[RetrievalTrace]:
    return session.added_of(RetrievalTrace)


@pytest.fixture(autouse=True)
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每条测试一个干净的临时日志目录（与 ``test_retrieval_log_hygiene`` 同一套做法）。"""

    log_dir = tmp_path / "logs"
    logging_mod = importlib.import_module("app.core.logging")
    config_mod = importlib.import_module("app.core.config")
    monkeypatch.setattr(config_mod.settings, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(config_mod.settings, "LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logging_mod, "_configured", False)
    logging_mod.setup_logging()
    yield log_dir
    logging.shutdown()
    logging_mod._configured = False
    logging_mod.setup_logging(log_dir=tmp_path / "logs-restored", log_level="INFO")


def _records(log_dir: Path, filename: str = "app.log") -> list[dict[str, Any]]:
    path = log_dir / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ──────────────────────────────────────────────────── 派生：纯函数，不依赖旁路


def test_the_same_log_id_always_derives_the_same_trace_id() -> None:
    """同一个 ``log_id`` 恒等——否则「按 trace_id 回查」就要求第二张对照表。"""

    log_id = "3f2c1a4e-8b90-4d11-95c7-2a1f6b0d9e33"

    assert trace_id_for(log_id) == trace_id_for(log_id)
    assert trace_id_for(log_id) != trace_id_for("3f2c1a4e-8b90-4d11-95c7-2a1f6b0d9e34")


def test_trace_id_is_thirty_two_hexadecimal_characters() -> None:
    """契约写的是「32 位十六进制」，不是「像十六进制的一串」。"""

    derived = trace_id_for("a3f9")

    assert len(derived) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", derived), derived


def test_delivered_chunk_ids_keep_the_representative_then_its_sources() -> None:
    """``chunk_ids`` 存的是**交付单元**的 id：代表片在前，溯源片随后，重复只留一次。"""

    results = [
        {"chunk_id": "rep-1", "source_chunk_ids": ["rep-1", "src-1", "src-2"]},
        {"chunk_id": "rep-2", "source_chunk_ids": []},
        {"chunk_id": "rep-3"},
    ]

    assert delivered_chunk_ids(results) == ["rep-1", "src-1", "src-2", "rep-2", "rep-3"]


# ──────────────────────────────────────────────────────── 响应：trace_id 永远在


def test_every_response_carries_a_thirty_two_hex_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方要的是「能写进自己日志、报障时能对账」的 id：稳定、非空、不依赖外部服务。"""

    with _offline_client(monkeypatch) as fake:
        body = _retrieve(fake["client"])

    trace_id = body["data"]["metadata"]["trace_id"]

    assert isinstance(trace_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id), trace_id


def test_metadata_gains_exactly_one_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """加字段的位置在顶层，且**只加这一个**：``retrieval`` 里一字未动。"""

    with _offline_client(monkeypatch) as fake:
        metadata = _retrieve(fake["client"])["data"]["metadata"]

    assert set(metadata) == {
        "mode",
        "top_k",
        "vector_store",
        "retrieval",
        "tenant_policy",
        "trace_id",
    }
    assert set(metadata["retrieval"]) == {
        "mode",
        "degraded",
        "degraded_reason",
        "empty_reason",
        "rerank",
    }


# ──────────────────────────────────────────────────── 写入：一行、独立事务、可对上


def test_one_row_lands_and_mirrors_what_the_caller_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入成功路径：一行落库，身份与响应逐项对得上，且这行走的是独立的会话。"""

    with _offline_client(monkeypatch) as fake:
        body = _retrieve(fake["client"])
    session = fake["trace_session"]
    rows = _trace_rows(session)

    assert len(rows) == 1, "一次检索只留一行"
    row = rows[0]
    metadata = body["data"]["metadata"]
    chunks = body["data"]["retrieved_chunks"]

    assert row.trace_id == metadata["trace_id"], "响应里的那根线必须能在库里找到"
    assert row.id != row.trace_id, "log_id 与 trace_id 是两个 id，不是同一个"
    assert row.tenant_id == TENANT_ID
    assert row.kb_ids == [KB_ID]
    assert row.chunk_ids == [chunk["chunk_id"] for chunk in chunks]
    assert row.top_k == metadata["top_k"] == len(chunks)
    assert isinstance(row.latency_ms, int) and row.latency_ms >= 0
    assert row.degraded_reason is None
    assert row.empty_reason is None
    # ``created_at`` 由**列默认值**在 flush 时补齐，假会话不 flush，所以这一项在
    # ``test_the_index_keeps_only_the_enumerated_columns`` 里按模型定义断言。
    # 独立短事务：自己 commit、自己 close，请求会话一次都没被拿来写。
    assert session.commits == 1
    assert session.closed is True


def test_the_empty_reason_of_this_call_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``empty_reason`` 取自本次 ``metadata.retrieval``，不是另算一遍。"""

    with _offline_client(monkeypatch, rows=[]) as fake:
        body = _retrieve(fake["client"])
    rows = _trace_rows(fake["trace_session"])

    assert body["data"]["metadata"]["retrieval"]["empty_reason"] == "no_chunks_matched"
    assert len(rows) == 1
    assert rows[0].empty_reason == "no_chunks_matched"
    assert rows[0].chunk_ids == []


def test_the_degraded_reason_of_this_call_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降级的那一次同样留一行，``degraded_reason`` 仍取自本次 ``metadata.retrieval``。

    混合模式里关键词引擎是空实现（``KEYWORD_SEARCH_PROVIDER=none``）→ 这一路记为降级，
    但整次检索仍然成功。写入方不许自己去猜原因，只许抄响应里那个值。
    """

    with _offline_client(monkeypatch) as fake:
        body = _retrieve(fake["client"], mode="hybrid")
    rows = _trace_rows(fake["trace_session"])

    assert body["data"]["metadata"]["retrieval"]["degraded_reason"] == "keyword search failed"
    assert len(rows) == 1
    assert rows[0].degraded_reason == "keyword search failed"
    assert rows[0].empty_reason is None


def test_a_failed_write_still_returns_the_trace_id_and_only_warns(
    monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    """观测是旁路：开不出会话也只留 warning，响应形状与 ``trace_id`` 一样不少。"""

    def explode() -> None:
        raise RuntimeError("psycopg: connection refused")

    with _offline_client(monkeypatch, session_factory=explode) as fake:
        body = _retrieve(fake["client"])

    assert body["code"] == 0
    trace_id = body["data"]["metadata"]["trace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id), "写库失败不该让这个字段缺席"

    records = _records(log_dir)
    warnings = [r for r in records if r.get("msg") == "retrieval trace not recorded"]
    assert len(warnings) == 1, "失败只留一条 warning"
    assert warnings[0].get("level") == "warning"
    assert warnings[0].get("trace_id") == trace_id, "warning 里要能看出是哪一次"
    assert not _records(log_dir, "error.log"), "旁路失败不是 ERROR"

    raw = "".join(path.read_text(encoding="utf-8") for path in log_dir.glob("*.log*"))
    assert QUERY_SENTINEL not in raw, "warning 里不许夹带问句原文"


# ─────────────────────────────────────────────────────────── 表：逐列扫描，不抽查


def test_the_index_keeps_only_the_enumerated_columns() -> None:
    """这张表跟业务库同生共死：它上面的每一列都是长期承诺，多一列就是多一份泄露面。"""

    columns = {column.key for column in RetrievalTrace.__table__.columns}

    assert RetrievalTrace.__tablename__ == "retrieval_traces"
    assert columns == set(EXPECTED_COLUMNS)
    for word in FORBIDDEN_COLUMN_WORDS:
        assert not [name for name in columns if word in name], f"索引里不该有 {word} 一类的列"
    assert RetrievalTrace.__table__.columns["created_at"].default is not None, (
        "写入方不传时间：``created_at`` 必须由列默认值补齐"
    )


def test_no_column_of_the_written_row_carries_the_question_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐列扫描**真实写入的那一行**：哨兵问句与切片正文一个字都不许出现。"""

    with _offline_client(monkeypatch) as fake:
        _retrieve(fake["client"])
    rows = _trace_rows(fake["trace_session"])
    assert len(rows) == 1

    row = rows[0]
    scanned = {}
    for column in RetrievalTrace.__table__.columns:
        scanned[column.key] = str(getattr(row, column.key))
    assert set(scanned) == set(EXPECTED_COLUMNS), "每一列都要真的扫过，不能挑几列"

    for name, value in scanned.items():
        assert QUERY_SENTINEL not in value, f"{name} 里出现了问句原文"
        assert CONTENT_SENTINEL not in value, f"{name} 里出现了切片正文"


def test_the_old_table_name_survives_only_where_history_requires_it() -> None:
    """旧表名只许留在 initial 迁移与改名迁移里——其余代码与迁移清单一律用新名。"""

    offenders: list[str] = []
    paths = sorted((REPO_ROOT / "app").rglob("*.py")) + sorted(
        (REPO_ROOT / "migrations").rglob("*.py")
    )
    for path in paths:
        relative = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        if not _OLD_NAME_RE.search(text):
            continue
        if relative in _OLD_NAME_IN_HISTORY:
            continue
        # 改名迁移必须点出旧名（rename_table 的源表），它是唯一合法的另一处。
        if relative.parent == Path("migrations") / "versions" and "rename" in relative.name:
            continue
        offenders.append(str(relative))

    assert offenders == [], f"这些文件还在用旧表名：{offenders}"
    assert "retrieval_logs" not in Base.metadata.tables
