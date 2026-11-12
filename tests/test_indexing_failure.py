"""T3 · 异步上传契约：ASGI 边界验收（重写旧的同步索引失败留痕断言）。

异步化后上传只做登记：响应固定 ``status=3`` / ``chunk_count=0``，请求线程不解析、
不向量化，因此**不会**再产生 ``document indexed`` / ``document indexing failed``
记录。真正的失败留痕与脱敏文案由 T4 的 Worker 任务测试承接；本文件只守住「请求
线程不索引」这条新契约，以及登记日志只带业务标识、不带文档内容。

唯一接缝：ASGI 应用边界。用 TestClient 发真实上传请求，只观察统一响应体与
``logs/app.log`` / ``logs/error.log`` 的外部副作用。数据库依赖、知识库仓储、
投递缝与文档 ID 用框架官方依赖覆盖 / monkeypatch 换成离线测试替身；投递缝被
monkeypatch 成记录器，因此本文件不需要 Broker。

日志断言照 T2 约定：显式 monkeypatch ``settings.LOG_DIR`` 指向 tmp 目录、重置
``app.core.logging._configured`` 再跑 ``setup_logging()``；绝不依赖全局默认目录
（T2 的 teardown 会把它留在 pytest 临时目录里）。
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.utils.status import DocumentStatus
from tests.fake_db import ZERO_COUNT

# 调用方提交的文档内容：绝不允许出现在响应体或任何应用日志里。
DOCUMENT_CONTENT = "T3-CONTENT-SENTINEL"
# 异步化后请求线程永远不该写的两类索引日志。
INDEX_LOG_MESSAGES = ("document indexed", "document indexing failed")
ACCEPTED_LOG_MESSAGE = "documents accepted"

# 占位 Key 解出的默认租户；假仓储的租户必须与它一致才走得到登记分支。
TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"


@pytest.fixture(autouse=True)
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每条测试一个干净的临时日志目录，结束后还原成可用的全局配置。"""

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


def _records(path: Path) -> list[dict[str, Any]]:
    """读 JSON Lines 日志文件；不存在或为空视为零条。"""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _FakeSession:
    """最小会话替身：文档行留在内存里，不碰数据库。"""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.documents: dict[str, Any] = {}
        self.commit_count = 0

    def add(self, doc: Any) -> None:
        self.documents[doc.id] = doc

    def flush(self) -> None:
        return None

    def execute(self, _statement: Any) -> Any:
        return ZERO_COUNT

    def get(self, _model: Any, document_id: str) -> Any:
        return self.documents.get(document_id)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def offline_upload(monkeypatch: pytest.MonkeyPatch):
    """离线上传接缝：返回 ``(client, session, enqueued)``，投递缝只记录不投递。"""

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    document_service = importlib.import_module("app.services.document_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    session = _FakeSession()
    enqueued: list[dict[str, Any]] = []

    monkeypatch.setattr(
        kb_repo,
        "get_by_id_for_tenant",
        lambda db, kb_id, *, tenant_id: SimpleNamespace(
            tenant_id=tenant_id,
            split_strategy="structure",
            allowed_formats=["markdown"],
            settings={},
        ),
    )
    monkeypatch.setattr(document_service, "new_document_id", lambda: "doc-1")
    monkeypatch.setattr(
        document_service, "enqueue_document", lambda **kwargs: enqueued.append(kwargs)
    )

    main.app.dependency_overrides[session_mod.get_db] = lambda: session
    from app.api.deps import get_current_key
    from app.core.auth import DevKeyContext

    main.app.dependency_overrides[get_current_key] = lambda: DevKeyContext(
        tenant_id=settings.DEV_TENANT_ID
    )

    try:
        with TestClient(main.app) as test_client:
            yield test_client, session, enqueued
    finally:
        main.app.dependency_overrides.clear()


def _upload_payload() -> dict[str, str]:
    return {
        "kb_id": KB_ID,
        "title": "demo",
        "content": DOCUMENT_CONTENT,
    }


def _upload(client: TestClient):
    return client.post("/api/v1/documents/upload", json=_upload_payload())


def test_upload_registers_processing_and_never_writes_index_logs(
    offline_upload, log_dir: Path
) -> None:
    """上传只登记：status=3、chunk_count=0、投递一次，且请求线程不写索引日志。"""

    client, session, enqueued = offline_upload

    response = _upload(client)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert body["data"] == {
        "document_id": "doc-1",
        "kb_id": KB_ID,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "error_message": None,
        "source_type": "markdown",
    }
    assert session.commit_count == 1, "登记提交一次即返回，不写索引结果"
    assert session.documents["doc-1"].status == DocumentStatus.PROCESSING
    assert len(enqueued) == 1, "登记后必须投递一次索引任务"
    assert enqueued[0]["document_id"] == "doc-1"
    assert enqueued[0]["tenant_id"] == TENANT_ID
    assert enqueued[0]["kb_id"] == KB_ID

    index_records = [
        record
        for record in _records(log_dir / "app.log") + _records(log_dir / "error.log")
        if record.get("msg") in INDEX_LOG_MESSAGES
    ]
    assert index_records == [], "请求线程不做索引，不该出现索引成功 / 失败日志"
    assert not _records(log_dir / "error.log"), "成功登记不应产生 ERROR 记录"

    accepted = [
        record
        for record in _records(log_dir / "app.log")
        if record.get("msg") == ACCEPTED_LOG_MESSAGE
    ]
    assert len(accepted) == 1, "每次登记都要有一条 accepted 日志"
    record = accepted[0]
    assert record["level"] == "info"
    assert set(record) >= {
        "time",
        "level",
        "logger",
        "msg",
        "document_id",
        "tenant_id",
        "kb_id",
        "source_type",
    }
    assert record["document_id"] == "doc-1"
    assert record["tenant_id"] == TENANT_ID
    assert record["kb_id"] == KB_ID
    assert record["source_type"] == "markdown"
    assert DOCUMENT_CONTENT not in json.dumps(record), "文档内容不得进日志"


def test_response_never_echoes_content_or_internal_detail(offline_upload) -> None:
    """响应只回标识与状态：不回显正文、脱敏失败文案或任何内部细节。"""

    client, _session, _enqueued = offline_upload

    response = _upload(client)

    assert response.status_code == 200
    assert DOCUMENT_CONTENT not in response.text, "响应不应复制文档内容"
    assert "document indexing failed" not in response.text, "请求线程没有索引失败可报"
    assert "Traceback" not in response.text
    body = response.json()
    assert set(body["data"]) == {
        "document_id",
        "kb_id",
        "status",
        "chunk_count",
        "error_message",
        "source_type",
    }


def test_application_logs_never_carry_content_query_or_api_key(
    offline_upload, log_dir: Path
) -> None:
    """登记日志只带 ID / 派生量，不带文档内容、检索文本或 API Key。"""

    client, _session, _enqueued = offline_upload

    _upload_with_api_key(client)

    raw = (log_dir / "app.log").read_text(encoding="utf-8")
    assert DOCUMENT_CONTENT not in raw, "文档内容不得进应用日志"
    assert "T3-API-KEY-SENTINEL" not in raw, "API Key 不得进应用日志"
    for record in _records(log_dir / "app.log"):
        if record.get("msg") == ACCEPTED_LOG_MESSAGE:
            assert "query" not in record, "检索文本原文不得进日志"


def _upload_with_api_key(client: TestClient):
    return client.post(
        "/api/v1/documents/upload",
        json=_upload_payload(),
        headers={"X-Api-Key": "T3-API-KEY-SENTINEL"},
    )
