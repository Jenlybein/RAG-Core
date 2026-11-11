"""#102 · 六段度量常开 + 三种去向：整条检索链路上的那一半。

四个接缝，全部**外部可观察**：

* JSONL 文件本身：一次检索一行、含 ``trace_id`` 与六段耗时、**不含原文**（整文件扫哨兵）；
* ASGI ``POST /api/v1/rag/retrieve`` 的响应字节：``jsonl`` 与 ``none`` 两种去向之下
  逐字节相同（摘掉 ``trace_id`` 后比较，它是每次调用都不同的派生 id）；
* 观测服务不可达时的响应：仍然 200、仍然有 ``trace_id``、只多一条 ``warning``；
* 组合根注入的收集器（``retrieve(stage_timer=...)``）：去向是 ``none`` 时六段**照样**
  被算出——这是「关掉观测不等于关掉计时」唯一能被钉住的形状。

收集器是显式参数而不是全局状态，所以在同一次进程里既能被注入、也能被断言；文件与响应
两侧都不需要「先打开观测开关」才能观察。数据库与 Provider 一律是离线替身：不连 PG、
不连 ES、不发网络。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.observability import set_trace_sink
from app.observability.langfuse_sink import LangfuseTraceSink
from app.observability.stage_timing import (
    STAGE_COLLAPSE,
    STAGE_FUSION,
    STAGE_KEYWORD_RECALL,
    STAGE_QUERY_OPTIMIZATION,
    STAGE_RERANK,
    STAGE_VECTOR_RECALL,
    StageTimer,
)
from app.services.retrieval_options import RetrievalOptions
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_obs"
KB_ID = "kb-obs"

#: 问句与切片正文的哨兵：它们真的走过整条链路，而观测文件里一个字都不许出现。
QUERY_SENTINEL = "T102-QUERY-SENTINEL-7d2a"
CONTENT_SENTINEL = "T102-CONTENT-SENTINEL-3b6f"

IDENTITY = RetrievalIdentity(TENANT_ID)

#: ``metadata.trace_id`` 每次调用都不同：比对响应字节前先整段摘掉（与 #101 同一做法）。
_TRACE_ID_RE = re.compile(r',"trace_id":"[0-9a-f]{32}"')


# ────────────────────────────────────────────────────────────────── 离线替身


class _FakeResult:
    """``SELECT`` 的替身：count 要一个数，meta / unit 查询要一组行（这里给空）。"""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def all(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:
        return iter(())


class _FakeDb:
    """请求会话与向量分支会话共用：只回答「库里有多少条可检索切片」。"""

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(1)

    def close(self) -> None:
        pass


class _FakeEmbedding:
    async def embed_query(self, query: str) -> list[float]:
        assert query == QUERY_SENTINEL, "哨兵必须真的进入检索链路，否则下面的断言是空断言"
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    async def similarity_search_scope(
        self, db, query_vector, *, tenant_id, index_versions, top_k
    ) -> list[dict]:
        (kb_id,) = index_versions
        return [
            {
                "document_id": "doc-obs",
                "chunk_id": f"chunk-{index}",
                "title": "部署实施指南",
                "content": CONTENT_SENTINEL,
                "score": 0.9 - index / 10,
                "kb_id": kb_id,
            }
            for index in range(2)
        ][:top_k]

    async def similarity_search(self, *args: Any, **kwargs: Any) -> list[dict]:  # pragma: no cover
        raise AssertionError("检索走范围版，不许按库循环")


class _FakeKeyword:
    """关键词引擎替身：可用、返回一行（单模式 bm25 与混合模式都用它）。"""

    available = True

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k) -> list[dict]:
        return [
            {
                "document_id": "doc-obs",
                "chunk_id": "chunk-kw",
                "title": "部署实施指南",
                "content": CONTENT_SENTINEL,
                "score": 9.0,
            }
        ][:top_k]


class _FakeRerankProvider:
    """重排替身：可用，原样返回（这一步发生过就够了）。"""

    available = True

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        return list(chunks)[:top_n]


class _ExplodingLangfuseClient:
    """服务不可达的观测后端：SDK 一碰就抛。"""

    def start_as_current_observation(self, **_kwargs: Any) -> Any:
        raise RuntimeError("langfuse is unreachable")


# ────────────────────────────────────────────────────────────────── 装配


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keyword: Any = None,
    rerank: Any = None,
) -> Iterator[TestClient]:
    """离线 ASGI 客户端；旁路写库的会话由 conftest 的进程级替身接管。"""

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    service = importlib.import_module("app.services.rag_service")
    pipeline = importlib.import_module("app.services.retrieval.pipeline")
    rerank_module = importlib.import_module("app.services.retrieval.rerank")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore())
    monkeypatch.setattr(
        service,
        "get_keyword_search_provider",
        lambda: keyword if keyword is not None else _FakeKeyword(),
    )
    if rerank is not None:
        # 重排模块在 import 时就把工厂名字绑进了自己的命名空间，所以替换点在那里。
        monkeypatch.setattr(rerank_module, "get_rerank_provider", lambda: rerank)
    # 向量分支在自己的线程里取会话，与请求会话不是同一个（既有接缝）。
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(
        kb_repo,
        "get_by_id_for_tenant",
        lambda db, kb_id, *, tenant_id: SimpleNamespace(
            tenant_id=tenant_id, settings={}, name="", description=""
        ),
    )

    from app.api.deps import get_current_key

    main.app.dependency_overrides[session_mod.get_db] = lambda: _FakeDb()
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield client
    finally:
        main.app.dependency_overrides.clear()


def _retrieve(client: TestClient, *, mode: str = "vector", top_k: int = 2) -> dict:
    """缺省走 ``vector`` 单模式、不折叠：结果是两片小片，形状稳定。"""

    IDENTITY.use(mode=mode, top_k=top_k, include_parent=False)
    response = client.post(RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": QUERY_SENTINEL})
    assert response.status_code == 200, response.text
    return response.json()


def _jsonl_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("retrieval-traces-*.jsonl")):
        records.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        )
    return records


def _use_jsonl(monkeypatch: pytest.MonkeyPatch, directory: Path) -> Path:
    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    monkeypatch.setattr(settings, "TRACE_JSONL_DIR", str(directory))
    return directory


def _use_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRACE_SINK", "none")


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每条测试一个干净的临时日志目录（与 ``test_trace_identity`` 同一套做法）。"""

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


def _without_trace_id(text: str) -> str:
    return _TRACE_ID_RE.sub("", text)


# ───────────────────────────────────────────── jsonl 去向：一次检索一行、不含原文


def test_one_retrieval_leaves_exactly_one_line_without_any_source_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一次检索一行：身份、六段耗时、结构化事实；问句与正文一个字都不许进文件。"""

    traces = _use_jsonl(monkeypatch, tmp_path / "traces")

    with _offline_client(monkeypatch) as client:
        body = _retrieve(client)
        _retrieve(client)

    records = _jsonl_records(traces)
    assert len(records) == 2, "两次检索两行"

    record = records[0]
    assert record["trace_id"] == body["data"]["metadata"]["trace_id"], (
        "文件里那一行必须与响应里那根线是同一个 id"
    )
    assert record["mode"] == "vector"
    assert record["top_k"] == 2
    assert [span["name"] for span in record["spans"]] == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_VECTOR_RECALL,
        STAGE_COLLAPSE,
    ], "单模式没有关键词与融合，那两段就不该出现"
    assert all(span["duration_ms"] >= 0 for span in record["spans"])

    raw = "".join(path.read_text(encoding="utf-8") for path in traces.glob("*.jsonl"))
    assert QUERY_SENTINEL not in raw, "问句原文进了观测文件"
    assert CONTENT_SENTINEL not in raw, "切片正文进了观测文件"


def test_a_hybrid_retrieval_records_five_stages_without_rerank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """混合模式多出两路召回与融合；这一档不要重排，所以就没有重排那一段。"""

    traces = _use_jsonl(monkeypatch, tmp_path / "traces")

    with _offline_client(monkeypatch) as client:
        _retrieve(client, mode="hybrid", top_k=2)

    record = _jsonl_records(traces)[0]
    assert [span["name"] for span in record["spans"]] == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_VECTOR_RECALL,
        STAGE_KEYWORD_RECALL,
        STAGE_FUSION,
        STAGE_COLLAPSE,
    ]


# ─────────────────────────────────── none 去向：不落文件，响应逐字节与 jsonl 一致


def test_the_none_sink_writes_no_file_and_keeps_the_response_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """切到「丢弃」去向：不产生文件，响应与接入本地文件时逐字节相同。

    ``trace_id`` 是每次调用都不同的派生 id（#101），比对前整段摘掉；除此之外任何一个
    字节的差别都说明观测层动了响应。
    """

    traces = _use_jsonl(monkeypatch, tmp_path / "traces")
    with _offline_client(monkeypatch) as client:
        IDENTITY.use(mode="hybrid", top_k=2, include_parent=False)
        response_jsonl = client.post(
            RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": QUERY_SENTINEL}
        )
    written = _jsonl_records(traces)
    assert written, "jsonl 那一侧必须真的写下了东西，否则比对没有意义"

    _use_none(monkeypatch)
    with _offline_client(monkeypatch) as client:
        IDENTITY.use(mode="hybrid", top_k=2, include_parent=False)
        response_none = client.post(
            RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": QUERY_SENTINEL}
        )

    assert response_none.status_code == response_jsonl.status_code == 200
    assert _without_trace_id(response_none.text) == _without_trace_id(response_jsonl.text)
    assert _TRACE_ID_RE.search(response_none.text), "摘除必须真的命中一次，否则是空比对"
    assert _jsonl_records(traces) == written, "none 去向不许再产出一行"


# ─────────────────────────────── langfuse 去向：服务不可达也只留 warning，检索照常


def test_an_unreachable_observability_service_only_leaves_a_warning(
    monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    """观测栈连不上：检索 200、``trace_id`` 照样有值、只多一条 warning。"""

    set_trace_sink(LangfuseTraceSink(client=_ExplodingLangfuseClient()))

    with _offline_client(monkeypatch) as client:
        response = client.post(RETRIEVE_PATH, json={"kb_ids": [KB_ID], "query": QUERY_SENTINEL})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert re.fullmatch(r"[0-9a-f]{32}", body["data"]["metadata"]["trace_id"])

    warnings = [
        record
        for record in _records(log_dir)
        if record.get("msg") == "observability trace not recorded"
    ]
    assert len(warnings) == 1, "一次失败只留一条 warning"
    assert warnings[0]["level"] == "warning"
    assert warnings[0]["trace_id"] == body["data"]["metadata"]["trace_id"]
    assert not _records(log_dir, "error.log"), "旁路失败不是 ERROR"

    raw = "".join(path.read_text(encoding="utf-8") for path in log_dir.glob("*.log*"))
    assert QUERY_SENTINEL not in raw, "warning 里不许夹带问句原文"


# ────────────────────────────── 度量常开：去向是 none 时六段仍然被算出（注入收集器）


def _options(*, mode: str, rerank: bool = False, top_k: int = 2) -> RetrievalOptions:
    """直接造一份已解析参数：本文件要断言的是链路，不是套餐门禁。"""

    return RetrievalOptions(
        mode=mode,
        top_k=top_k,
        fold_to_sections=False,
        expand_table_rows=False,
        rerank=rerank,
        rewrite=False,
        tenant_policy=None,
    )


async def _retrieve_with_timer(
    monkeypatch: pytest.MonkeyPatch, *, mode: str, rerank: bool = False
) -> list[str]:
    """用**注入的**收集器跑一次组合根，返回记到的段名（顺序即报告顺序）。"""

    service = importlib.import_module("app.services.rag_service")
    pipeline = importlib.import_module("app.services.retrieval.pipeline")
    rerank_module = importlib.import_module("app.services.retrieval.rerank")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    monkeypatch.setattr(service, "get_embedding_provider", lambda: _FakeEmbedding())
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore())
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: _FakeKeyword())
    monkeypatch.setattr(pipeline, "SessionLocal", lambda: _FakeDb())
    if rerank:
        monkeypatch.setattr(rerank_module, "get_rerank_provider", lambda: _FakeRerankProvider())
    monkeypatch.setattr(
        kb_repo,
        "get_by_id_for_tenant",
        lambda db, kb_id, *, tenant_id: SimpleNamespace(
            tenant_id=tenant_id, settings={}, name="", description=""
        ),
    )

    timer = StageTimer()
    await service.retrieve(
        _FakeDb(),
        tenant_id=TENANT_ID,
        kb_ids=[KB_ID],
        query=QUERY_SENTINEL,
        options=_options(mode=mode, rerank=rerank),
        stage_timer=timer,
    )
    return [span.name for span in timer.spans()]


def test_the_stages_are_measured_even_when_nothing_collects_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """去向是 ``none``，收集器是注入的：六段**照样**被算出（父 spec「度量常开」）。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "none")

    stages = asyncio.run(_retrieve_with_timer(monkeypatch, mode="hybrid", rerank=True))

    assert stages == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_VECTOR_RECALL,
        STAGE_KEYWORD_RECALL,
        STAGE_FUSION,
        STAGE_RERANK,
        STAGE_COLLAPSE,
    ]


def test_a_stage_that_did_not_happen_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没走过的段不补 0：``vector`` 没有关键词与融合；单模式没有重排。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "none")

    assert asyncio.run(_retrieve_with_timer(monkeypatch, mode="vector")) == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_VECTOR_RECALL,
        STAGE_COLLAPSE,
    ]
    assert asyncio.run(_retrieve_with_timer(monkeypatch, mode="bm25")) == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_KEYWORD_RECALL,
        STAGE_COLLAPSE,
    ]
