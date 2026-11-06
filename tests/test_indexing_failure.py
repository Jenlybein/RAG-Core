"""T6 · 索引链路失败留痕：ASGI 边界验收。

唯一接缝：ASGI 应用边界。用 TestClient 发真实上传请求，只观察统一响应体与
``logs/app.log`` / ``logs/error.log`` 的外部副作用。数据库依赖、知识库仓储、
索引编排与文档状态回写用框架官方依赖覆盖 / monkeypatch 换成离线测试替身，
让同一条接缝不发真实模型请求、不连 PostgreSQL——不新增第二条生产接缝。

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

# 内部异常原文：允许出现在日志里（排障线索），绝不允许出现在响应体里。
INTERNAL_DETAIL = "T6-INTERNAL-EXCEPTION-SENTINEL"
# 调用方提交的文档内容：绝不允许出现在任何应用日志里。
DOCUMENT_CONTENT = "T6-CONTENT-SENTINEL"
# 固定脱敏文案（外部契约）：不含内部异常原文，也不含文档内容。
SANITIZED_MESSAGE = "document indexing failed, please retry or contact support"

# 占位 Key 解出的默认租户；假仓储的租户必须与它一致才走得到成功分支。
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
        self.documents: dict[str, Any] = {}

    def add(self, doc: Any) -> None:
        self.documents[doc.id] = doc

    def flush(self) -> None:
        return None

    def get(self, _model: Any, document_id: str) -> Any:
        return self.documents.get(document_id)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeIndexingService:
    """索引编排替身：默认成功；``index`` 可被单条测试换成抛异常。"""

    async def index(self, db, *, doc, content) -> int:
        return 3


@pytest.fixture
def offline_upload(monkeypatch: pytest.MonkeyPatch):
    """离线上传接缝：返回 ``(client, session)``，并允许替换索引编排行为。

    知识库仓储 / 文档状态回写 / ID 生成 / 编排用 monkeypatch 固定，数据库用
    框架自带的 ``dependency_overrides`` 替换；这些只是让 ASGI 这一条接缝离线。
    """

    main = importlib.import_module("app.main")
    session_mod = importlib.import_module("app.db.session")
    document_service = importlib.import_module("app.services.document_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    session = _FakeSession()

    monkeypatch.setattr(
        kb_repo, "get_by_id", lambda db, kb_id: SimpleNamespace(tenant_id=TENANT_ID)
    )
    monkeypatch.setattr(document_service, "new_document_id", lambda: "doc-1")
    monkeypatch.setattr(document_service, "IndexingService", _FakeIndexingService)
    main.app.dependency_overrides[session_mod.get_db] = lambda: session
    from app.core.auth import DevKeyContext, resolve_dev_key
    from app.core.config import settings

    main.app.dependency_overrides[resolve_dev_key] = lambda: DevKeyContext(
        tenant_id=settings.DEV_TENANT_ID
    )

    try:
        with TestClient(main.app) as test_client:
            yield test_client, session
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


def _explode(self, db, *, doc, content) -> int:
    raise RuntimeError(INTERNAL_DETAIL)


async def _success_index(self, db, *, doc, content) -> int:
    return 3


def test_failed_index_keeps_success_envelope_and_sanitizes_error_message(
    offline_upload, monkeypatch, log_dir: Path
) -> None:
    """索引失败仍是 code=0 的成功信封 + status=FAILED，原因文案脱敏。"""

    client, _session = offline_upload

    monkeypatch.setattr(_FakeIndexingService, "index", _explode)

    response = _upload(client)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert body["data"]["status"] == 2  # FAILED
    assert body["data"]["chunk_count"] == 0
    assert body["data"]["document_id"] == "doc-1"
    assert body["data"]["kb_id"] == KB_ID
    assert body["data"]["error_message"] == SANITIZED_MESSAGE
    assert INTERNAL_DETAIL not in response.text, "内部异常原文绝不能回显给调用方"
    assert DOCUMENT_CONTENT not in response.text, "响应不应复制文档内容"


def test_failed_index_writes_error_log_with_business_ids(
    offline_upload, monkeypatch, log_dir: Path
) -> None:
    """静默吞掉的失败现在留痕：ERROR 文件带三个业务标识与异常堆栈。"""

    client, _session = offline_upload

    monkeypatch.setattr(_FakeIndexingService, "index", _explode)

    _upload(client)

    error_records = _records(log_dir / "error.log")
    failures = [r for r in error_records if r.get("msg") == "document indexing failed"]
    assert len(failures) == 1, "每次索引失败都要有一条 ERROR 记录"
    failure = failures[0]
    assert failure["level"] == "error"
    assert failure["document_id"] == "doc-1"
    assert failure["tenant_id"] == TENANT_ID
    assert failure["kb_id"] == KB_ID
    assert INTERNAL_DETAIL in json.dumps(failure), "内部异常细节只进日志，供排障回查"
    assert DOCUMENT_CONTENT not in json.dumps(failure), "文档内容不得进日志"
    assert "query" not in failure, "检索文本原文不得进日志"


def test_successful_index_logs_business_ids_for_comparison(offline_upload, log_dir: Path) -> None:
    """成功路径也带同样的业务标识与派生量，便于与失败记录对照。"""

    client, _session = offline_upload

    response = _upload(client)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == 1  # SUCCESS
    successes = [
        r
        for r in _records(log_dir / "app.log")
        if r.get("msg") == "document indexed" and r.get("level") == "info"
    ]
    assert len(successes) == 1, "每次索引成功都要留一条对照日志"
    success = successes[0]
    assert success["document_id"] == "doc-1"
    assert success["tenant_id"] == TENANT_ID
    assert success["kb_id"] == KB_ID
    assert success["chunk_count"] == 3
    assert DOCUMENT_CONTENT not in json.dumps(success), "文档内容不得进日志"
    assert not _records(log_dir / "error.log"), "成功路径不应产生 ERROR 记录"


def test_failed_index_row_is_written_back_and_retryable(offline_upload, monkeypatch) -> None:
    """失败后文档行仍为 FAILED 且已提交；重试同一条链路可以成功。"""

    client, session = offline_upload
    calls: list[int] = []

    async def flaky(self, db, *, doc, content) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(INTERNAL_DETAIL)
        return 3

    monkeypatch.setattr(_FakeIndexingService, "index", flaky)

    failed = _upload(client)
    assert failed.json()["data"]["status"] == 2
    row = session.documents["doc-1"]
    assert row.status == 2, "失败状态必须回写并保留，供调用方轮询"
    assert row.error_message == SANITIZED_MESSAGE
    assert INTERNAL_DETAIL not in str(row.error_message)

    retried = _upload(client)
    assert retried.json()["data"]["status"] == 1
    assert retried.json()["data"]["chunk_count"] == 3
    assert retried.json()["data"]["error_message"] is None
    assert session.documents["doc-1"].status == 1


def test_application_logs_never_carry_content_query_or_api_key(
    offline_upload, monkeypatch, log_dir: Path
) -> None:
    """失败与成功两条路径的日志只带 ID / 派生量，不带文档内容、检索文本或 API Key。"""

    client, _session = offline_upload

    monkeypatch.setattr(_FakeIndexingService, "index", _explode)
    _upload_with_api_key(client)
    # 换回默认成功实现，再发一次请求对照。
    monkeypatch.setattr(_FakeIndexingService, "index", _success_index)
    _upload_with_api_key(client)

    raw = (log_dir / "app.log").read_text(encoding="utf-8")
    assert DOCUMENT_CONTENT not in raw, "文档内容不得进应用日志"
    assert "T6-API-KEY-SENTINEL" not in raw, "API Key 不得进应用日志"
    for record in _records(log_dir / "app.log"):
        if record.get("msg") in ("document indexed", "document indexing failed"):
            assert set(record) >= {
                "time",
                "level",
                "logger",
                "msg",
                "document_id",
                "tenant_id",
                "kb_id",
            }
            assert "query" not in record, "检索文本原文不得进日志"


def _upload_with_api_key(client: TestClient):
    return client.post(
        "/api/v1/documents/upload",
        json=_upload_payload(),
        headers={"X-Api-Key": "T6-API-KEY-SENTINEL"},
    )
