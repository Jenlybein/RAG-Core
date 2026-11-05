"""T5 · 服务异常兜底与包壳边界：ASGI 应用边界验收。

唯一接缝：TestClient 发真实请求，只看状态码、响应体、响应头与日志文件副作用。
触发未捕获异常用 monkeypatch 换掉业务模块级函数；数据库依赖用框架官方的
dependency_overrides 替换——两者只是让同一条接缝离线，不新增生产端点。

日志断言不依赖全局默认目录：T2 的模块级 autouse teardown 会把全局 LOG_DIR
留在 pytest 临时目录，因此本文件每条测试都显式把 settings.LOG_DIR 指向自己的
tmp 目录、重置 app.core.logging._configured、再跑一次 setup_logging()。
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_VALID_PAYLOAD = {
    "tenant_id": "tenant-a",
    "kb_id": "kb-1",
    "user_id": "user-1",
    "query": "hello",
}

# 故意让异常消息里带上「内部细节」：它们只准出现在日志里，绝不准出现在响应体。
_INTERNAL_SENTINEL = "T5-INTERNAL-SENTINEL"
_INTERNAL_PATH = r"C:\secret\rag-core\internal.py"
_INTERNAL_SQL = "SELECT * FROM tenants WHERE api_key ="


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


def _records(path: Path) -> list[dict]:
    """读一个 JSON Lines 文件；不存在或为空视为零条。"""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """离线 ASGI 边界；500 会被 ServerErrorMiddleware 重新抛出，须关掉测试客户端的再抛出。"""

    main = importlib.import_module("app.main")
    session = importlib.import_module("app.db.session")
    main.app.dependency_overrides[session.get_db] = lambda: object()
    try:
        with TestClient(main.app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


def _make_service_explode(monkeypatch: pytest.MonkeyPatch) -> None:
    """让检索业务抛出一个未捕获异常（不新增任何端点）。"""

    service = importlib.import_module("app.services.rag_service")

    async def explode(*args, **kwargs):
        raise RuntimeError(f"{_INTERNAL_SENTINEL} | {_INTERNAL_PATH} | {_INTERNAL_SQL}")

    monkeypatch.setattr(service, "retrieve", explode)


def _trigger_500(client: TestClient, monkeypatch: pytest.MonkeyPatch, **kwargs):
    _make_service_explode(monkeypatch)
    return client.post("/api/v1/rag/retrieve", json=_VALID_PAYLOAD, **kwargs)


# ── 服务异常：HTTP 500 + 系统错误码，内部细节只进日志 ──


def test_unhandled_exception_returns_500_with_system_code_and_no_internals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _trigger_500(client, monkeypatch)

    assert response.status_code == 500
    assert response.json() == {"code": 50000, "msg": "server error", "data": None}

    body = response.text.lower()
    for leaked in (
        "traceback",
        "runtimeerror",
        _INTERNAL_SENTINEL.lower(),
        "secret",
        "select ",
        ".py",
    ):
        assert leaked not in body, f"响应体不得泄露内部细节：{leaked}"

    request_id = response.headers.get("x-request-id")
    assert request_id, "500 响应必须显式带上关联标识"
    assert 1 <= len(request_id) <= 64
    assert all(c.isalnum() or c in "_-" for c in request_id)


def test_unhandled_exception_writes_error_log_with_stack(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    response = _trigger_500(client, monkeypatch)
    assert response.status_code == 500

    records = [r for r in _records(log_dir / "error.log") if r.get("msg") == "unhandled exception"]
    assert len(records) == 1, "服务异常必须以一条 ERROR 记录落到 error.log"
    record = records[0]
    assert record["level"] == "error"
    stack = record.get("exception", "")
    assert "Traceback" in stack and "RuntimeError" in stack, "堆栈必须留在日志里"
    assert _INTERNAL_SENTINEL in stack, "异常细节只进日志"
    assert record["request_id"] == response.headers["x-request-id"]


def test_500_header_and_error_log_share_one_id_without_incoming_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    """没有入站标识时，自生成的号牌也必须同时出现在 500 响应头与 ERROR 日志里。

    这是真正的关联缺口：T3 的中间件在 finally 里清掉了 contextvars，最外层
    ServerErrorMiddleware 再调 500 handler 时已读不到绑定值，只能靠请求 scope 传递。
    """

    response = _trigger_500(client, monkeypatch)
    assert response.status_code == 500

    records = [r for r in _records(log_dir / "error.log") if r.get("msg") == "unhandled exception"]
    assert len(records) == 1
    assert records[0]["request_id"] == response.headers["x-request-id"]


def test_500_echoes_legal_incoming_request_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, log_dir: Path
) -> None:
    response = _trigger_500(client, monkeypatch, headers={"X-Request-ID": "t5-legal-id_7"})
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "t5-legal-id_7"

    records = [r for r in _records(log_dir / "error.log") if r.get("msg") == "unhandled exception"]
    assert len(records) == 1
    assert records[0]["request_id"] == "t5-legal-id_7"


# ── 包壳边界：探活 / 文档 / 规范 / 写错路径保持 FastAPI 原生 ──


def test_health_docs_openapi_and_wrong_path_stay_native(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert set(health.json()) == {"status"}

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert docs.headers["content-type"].startswith("text/html")
    assert "swagger" in docs.text.lower()

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"] == {"title": "RAG-Core", "version": "0.1.0"}
    assert "/api/v1/rag/retrieve" in schema.json()["paths"]

    missing = client.get("/nope")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
    assert set(missing.json()) == {"detail"}, "404 不得套三段式业务结构"

    missing_api_path = client.get("/api/v1/does-not-exist")
    assert missing_api_path.status_code == 404
    assert missing_api_path.json() == {"detail": "Not Found"}


def test_500_envelope_does_not_leak_into_docs_or_health(client: TestClient) -> None:
    """异常兜底处理器不改变探活与文档的响应形状。"""

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_global_exception_handler_envelopes_unhandled_error_outside_api_prefix(
    client: TestClient,
) -> None:
    """实测边界：Exception handler 全局注册，非业务路径一旦抛未捕获异常也会被包壳。

    包壳范围「只覆盖 /api/v1/*」是异常类型驱动的结果——业务与校验异常只由业务
    路由抛出，探活/文档/404 正常时保持原生形状。但生产 app 的 Exception handler
    不看路径：非业务路径真抛异常时，同样返回 500 + 50000。这里钉住真实行为，
    不把它写成「原生 404」。刻意不新增端点，只临时让 /health 的 endpoint 抛错。
    """

    main = importlib.import_module("app.main")
    health_route = next(
        route for route in main.app.routes if getattr(route, "path", None) == "/health"
    )
    original_call = health_route.dependant.call

    async def explode() -> None:
        raise RuntimeError(_INTERNAL_SENTINEL)

    health_route.dependant.call = explode
    try:
        response = client.get("/health")
    finally:
        health_route.dependant.call = original_call

    assert response.status_code == 500
    assert response.json() == {"code": 50000, "msg": "server error", "data": None}
    assert _INTERNAL_SENTINEL not in response.text
    request_id = response.headers.get("x-request-id")
    assert request_id, "500 响应必须显式带上关联标识"

    # 没有异常的普通请求仍是原生形状；边界由「是否抛异常」决定，不是响应包裹。
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/nope").json() == {"detail": "Not Found"}


# ── 无回归：业务失败与校验失败仍是 HTTP 200 ──


def test_business_and_validation_failures_still_return_http_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = importlib.import_module("app.repositories.knowledge_base_repository")
    monkeypatch.setattr(repo, "get_by_id", lambda db, kb_id: None)

    business = client.post("/api/v1/rag/retrieve", json=_VALID_PAYLOAD)
    assert business.status_code == 200
    assert business.json()["code"] == 20008

    validation = client.post("/api/v1/knowledge-bases/create", json={})
    assert validation.status_code == 200
    assert validation.json()["code"] == 20002
