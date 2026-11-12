"""T7 · 端到端检索日志卫生：验收闸门补上的唯一缺口。

唯一接缝仍是 ASGI 应用边界：TestClient 发一次**真实的检索请求**
``POST /api/v1/rag/retrieve``，只观察响应体与 ``logs/app.log`` / ``logs/error.log``
的外部副作用。数据库用框架官方 ``dependency_overrides`` 换掉，知识库仓储、
embedding 与向量库工厂用 monkeypatch 换成离线替身——不发真实模型请求、
不连 PostgreSQL、不新增生产端点。

T6 只在**上传链路**上、且只对自己产生的两条记录做过结构化断言；本文件把
spec #1 用户故事 32 的「应用日志不出现检索文本原文 / 文档内容 / API Key」
钉在**整条真实检索链路**上（成功 + 服务异常两个分支），并且扫描整份日志文件
的原始字节，而不是只挑某几条记录。

日志断言照 T2 约定：显式 monkeypatch ``settings.LOG_DIR`` 指向 tmp 目录、重置
``app.core.logging._configured``、再跑 ``setup_logging()``；绝不依赖全局默认目录。
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import structlog
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb
from tests.retrieval_identity import RetrievalIdentity

# 三个哨兵：请求带着它们走完整条检索链路，并且确实从响应体回给调用方。
QUERY_TEXT = "T7-QUERY-SENTINEL-9f3b"
DOCUMENT_CONTENT = "T7-DOC-CONTENT-SENTINEL"
API_KEY = "T7-API-KEY-SENTINEL"
# 服务异常分支：内部细节只允许进日志，用来证明日志真的来自本次请求。
INTERNAL_DETAIL = "T7-INTERNAL-DETAIL-SENTINEL"

# 占位 Key 解出的默认租户；假仓储/假 Provider 的租户必须与它一致。
TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"
REQUEST_ID = "t7-retrieval-id"

# 票 #77 起质量参数由密钥配置提供：请求助手把旧写法翻译成「造一个 custom 档的假身份」。
IDENTITY = RetrievalIdentity(TENANT_ID)


@pytest.fixture(autouse=True)
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每条测试一个干净的临时日志目录；结束时还原成可用的全局配置。"""

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


def _log_files_text(log_dir: Path) -> str:
    """整份日志（含可能的滚动文件）的原始文本；不存在视为空串。"""

    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted(log_dir.glob("*.log*"))
        if path.is_file()
    )


def _records(log_dir: Path, filename: str = "app.log") -> list[dict[str, Any]]:
    """读一个 JSON Lines 文件；不存在或为空视为零条。"""

    path = log_dir / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _FakeEmbeddingProvider:
    """离线 embedding 替身：不发出任何网络请求。

    ``fail=True`` 时让业务层抛未捕获异常，用来走 500 兜底分支。日志只放 ID 与
    派生量，与 ``document_service`` 的既有写法一致。
    """

    def __init__(self, *, api_key: str, fail: bool = False) -> None:
        self._api_key = api_key
        self._fail = fail

    async def embed_query(self, query: str) -> list[float]:
        # 应用日志只放 ID 与派生量：query 原文与 api_key 一个字都不许落盘。
        # ``query`` / ``self._api_key`` 只参与计算，绝不交给 logger。
        assert query and self._api_key, "替身确实收到了敏感值，断言才有意义"
        structlog.get_logger("t7.retrieval.embedding").info(
            "query embedded",
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            vector_dim=3,
        )
        if self._fail:
            raise RuntimeError(INTERNAL_DETAIL)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    """离线向量库替身：返回一片带文档内容的切片，不碰数据库。

    票 5 起检索走范围版（一次覆盖全部配对）；两处都记录，且都只记 ID 与派生量
    ——这个文件守的正是「日志里不许出现检索原文」。
    """

    async def similarity_search(self, db, query_vector, *, tenant_id, kb_id, top_k) -> list[dict]:
        structlog.get_logger("t7.retrieval.vectorstore").info(
            "chunks retrieved",
            tenant_id=tenant_id,
            kb_id=kb_id,
            top_k=top_k,
            returned=1,
        )
        return [{**_row(), "kb_id": kb_id}]

    async def similarity_search_scope(
        self, db, query_vector, *, tenant_id, index_versions, top_k
    ) -> list[dict]:
        structlog.get_logger("t7.retrieval.vectorstore").info(
            "chunks retrieved",
            tenant_id=tenant_id,
            kb_ids=sorted(index_versions),
            top_k=top_k,
            returned=1,
        )
        (kb_id,) = index_versions or (None,)
        return [{**_row(), "kb_id": kb_id}]


def _row() -> dict:
    return {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "title": "demo",
        "content": DOCUMENT_CONTENT,
        "score": 0.9,
    }


@pytest.fixture
def offline_retrieval(log_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """离线 ASGI 边界；500 会被 ServerErrorMiddleware 重新抛出，关掉客户端再抛出。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    config_mod = importlib.import_module("app.core.config")

    monkeypatch.setattr(config_mod.settings, "MODEL_API_KEY", API_KEY)
    monkeypatch.setattr(
        kb_repo, "get_by_id_for_tenant", async_visible_kb
    )
    monkeypatch.setattr(
        service,
        "get_embedding_provider",
        lambda: _FakeEmbeddingProvider(api_key=config_mod.settings.MODEL_API_KEY),
    )
    monkeypatch.setattr(service, "get_vector_store", lambda: _FakeVectorStore())
    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        EmptySession()
    )
    from app.api.deps import get_current_key

    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield client
    finally:
        main.app.dependency_overrides.clear()


def _retrieve(client: TestClient, *, mode: str | None = None):
    payload: dict[str, Any] = {
        "kb_ids": [KB_ID],
        "query": QUERY_TEXT,
    }
    # 票 #77：``mode`` 不再进请求体，改由密钥配置表达（custom 档）；调用点保持原样。
    # 每次都 ``use`` 一遍（``mode=None`` 时就是「服务端缺省模式」），用例之间不互相污染。
    IDENTITY.use(mode=mode)
    return client.post(
        "/api/v1/rag/retrieve",
        json=payload,
        headers={"X-Api-Key": API_KEY, "X-Request-ID": REQUEST_ID},
    )


def _assert_no_sensitive_values(log_dir: Path) -> None:
    """整份应用日志（app.log / error.log / 滚动文件）里不得出现三类哨兵。"""

    raw = _log_files_text(log_dir)
    assert raw, "本次请求必须真的产生日志，否则「没有敏感值」是空断言"
    for secret, label in (
        (QUERY_TEXT, "检索文本原文"),
        (DOCUMENT_CONTENT, "文档内容"),
        (API_KEY, "API Key"),
    ):
        assert secret not in raw, f"应用日志不得出现{label}"


def test_real_retrieval_logs_never_carry_query_content_or_api_key(
    offline_retrieval: TestClient, log_dir: Path
) -> None:
    """成功检索：哨兵走完链路并回给调用方，但一个字都不许留在应用日志里。"""

    client = offline_retrieval

    response = _retrieve(client)

    # 正向对照：哨兵确实进入了检索链路并回到响应体，断言不是「因为没数据才绿」。
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert body["data"]["query"] == QUERY_TEXT
    assert body["data"]["retrieved_chunks"][0]["content"] == DOCUMENT_CONTENT

    # 正向对照：日志里确实有本次请求产生的记录（按响应头里的号牌回查）。
    assert response.headers["x-request-id"] == REQUEST_ID
    bound = [r for r in _records(log_dir) if r.get("request_id") == REQUEST_ID]
    assert bound, "检索链路的日志应带上本次请求的关联标识"

    _assert_no_sensitive_values(log_dir)


def test_retrieval_server_error_logs_stay_clean(
    offline_retrieval: TestClient, monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    """服务异常：ERROR 日志留内部细节供排障，但检索原文 / 文档内容 / API Key 不进日志。"""

    client = offline_retrieval
    service = importlib.import_module("app.services.rag_service")
    config_mod = importlib.import_module("app.core.config")
    monkeypatch.setattr(
        service,
        "get_embedding_provider",
        lambda: _FakeEmbeddingProvider(api_key=config_mod.settings.MODEL_API_KEY, fail=True),
    )

    # 票 4 起缺省模式是 hybrid，而混合模式下「嵌入失败」是**契约规定的降级**
    # （对称降级：改用关键词一路，业务码仍成功），不再是 500。本测试守的是
    # 500 兜底分支的日志卫生，所以显式走不降级的单模式。
    response = _retrieve(client, mode="vector")

    assert response.status_code == 500
    assert response.json() == {"code": 50000, "msg": "server error", "data": None}

    # 正向对照：ERROR 日志非空、确属本次请求、且保留了内部异常细节。
    errors = [
        r
        for r in _records(log_dir, "error.log")
        if r.get("request_id") == response.headers["x-request-id"]
    ]
    assert len(errors) == 1
    assert INTERNAL_DETAIL in json.dumps(errors[0]), "内部细节只进日志，供排障回查"

    _assert_no_sensitive_values(log_dir)
