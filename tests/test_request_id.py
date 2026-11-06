"""请求关联标识（T3）验收测试。

唯一接缝：ASGI 应用边界。用 TestClient 发真实请求，只观察响应头、响应体与
``logs/app.log`` 的外部行为：每个响应带号牌、合法沿用 / 非法替换、日志同号牌、
响应体形状不变。不触碰中间件内部结构。

本项目所有响应一次性返回（MCP 入口已配 ``json_response=True``），因此中间件
改用 Starlette 自带的 ``BaseHTTPMiddleware``；「流不被缓冲」不再是本步的验收项。

T2 的日志 fixture 是模块级 autouse，只作用于 ``test_logging.py``；本文件
独立用 monkeypatch 把 ``settings.LOG_DIR`` 指向 tmp 目录，重置
``app.core.logging._configured`` 后重跑 ``setup_logging()``。
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


def _records(log_dir: Path) -> list[dict[str, Any]]:
    """读全量 JSON Lines 文件；不存在或为空视为零条。"""

    path = log_dir / "app.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def client() -> TestClient:
    """真实 ASGI 边界：被测对象就是应用本身（含已注册的中间件）。"""

    import app.main
    from app.core.auth import DevKeyContext, resolve_dev_key
    from app.core.config import settings

    app.main.app.dependency_overrides[resolve_dev_key] = lambda: DevKeyContext(
        tenant_id=settings.DEV_TENANT_ID
    )
    try:
        yield TestClient(app.main.app)
    finally:
        app.main.app.dependency_overrides.clear()


def test_every_response_carries_request_id(client: TestClient) -> None:
    """任意请求（含探活、文档、404、参数校验失败）的响应头都带关联标识。"""

    for path in ("/health", "/openapi.json", "/does-not-exist"):
        response = client.get(path)
        request_id = response.headers.get("x-request-id")
        assert request_id, f"{path} 的响应头缺少关联标识"
        assert 1 <= len(request_id) <= 64
        assert all(c.isalnum() or c in "_-" for c in request_id)

    invalid = client.post("/api/v1/knowledge-bases/create", json={})
    # ADR-0007 / T4：校验失败是业务失败，HTTP 200 + 20002，不再是框架默认的 422。
    assert invalid.status_code == 200
    assert invalid.json()["code"] == 20002
    assert invalid.headers.get("x-request-id"), "校验失败响应也要带号牌"


def test_legal_incoming_request_id_is_echoed_verbatim(client: TestClient) -> None:
    """客户端带合法标识时原样返回，不自生成新的。"""

    legal = "abc-123_XYZ"
    response = client.get("/health", headers={"X-Request-ID": legal})

    assert response.headers["x-request-id"] == legal


@pytest.mark.parametrize(
    "incoming",
    [
        None,
        "bad\nid",
        "bad id",
        "bad\tid",
        "bad/id",
        "x" * 65,
        "",
    ],
)
def test_illegal_or_missing_request_id_is_replaced_and_never_logged(
    client: TestClient, log_dir: Path, incoming: str | None
) -> None:
    """非法（控制字符/空格/斜杠/超长/空）或缺失的标识被替换，且原值不进 logs/app.log。"""

    headers = {} if incoming is None else {"X-Request-ID": incoming}
    response = client.get("/health", headers=headers)
    request_id = response.headers["x-request-id"]

    if incoming is not None:
        assert request_id != incoming, "非法标识不能被沿用"
    assert all(c.isalnum() or c in "_-" for c in request_id)
    assert 1 <= len(request_id) <= 64

    log_path = log_dir / "app.log"
    raw_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    if incoming:
        assert incoming not in raw_log, "非法标识绝不能写进日志（防日志注入）"


def test_all_log_lines_of_one_request_share_the_same_id(client: TestClient, log_dir: Path) -> None:
    """一次请求里应用日志、标准库日志、框架访问日志都带同一个关联标识。"""

    legal = "shared-id-42"

    # 在 ASGI 边界内追加一条临时路由：通过真实 HTTP 请求产生日志，
    # 仍然只观察 logs/app.log 的外部副作用。测试结束即移除，不污染全局 app。
    import app.main

    async def emit_logs() -> dict[str, str]:
        structlog.get_logger("t3.structlog").info("t3 first line", document_id="d-1")
        logging.getLogger("t3.stdlib").warning("t3 second line", extra={"kb_id": "k-1"})
        structlog.get_logger("t3.structlog").error("t3 third line", chunk_count=3)
        return {"status": "ok"}

    app.main.app.add_api_route("/__t3_logs", emit_logs, methods=["GET"], include_in_schema=False)
    try:
        response = client.get("/__t3_logs", headers={"X-Request-ID": legal})
        assert response.status_code == 200
        assert response.headers["x-request-id"] == legal
    finally:
        app.main.app.router.routes = [
            r for r in app.main.app.router.routes if getattr(r, "path", None) != "/__t3_logs"
        ]

    records = [r for r in _records(log_dir) if str(r.get("msg", "")).startswith("t3 ")]
    assert len(records) == 3, "请求内三条应用日志都应落盘"
    assert all(r.get("request_id") == legal for r in records), "全部日志行必须共享同一号牌"


def _mount_dummy_sub_app() -> str:
    """在根应用上挂一个极小的纯 ASGI 子应用，返回其访问路径。

    子应用故意不经过 FastAPI 的路由/依赖/异常处理器，模拟将来第二种接入面；
    它自己发一条 structlog 日志，用来验证 contextvars 跨挂载边界。
    """

    async def dummy_sub_app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        structlog.get_logger("t3.subapp").info("t3 mounted sub-app line", kb_id="k-sub")
        body = b'{"status":"ok"}'
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    path = "/__t3_mounted"
    main = importlib.import_module("app.main")
    main.app.mount(path, dummy_sub_app)
    return path


def test_mounted_sub_app_gets_request_id_and_logs_it(client: TestClient, log_dir: Path) -> None:
    """挂载的子应用同样被赋予标识：响应头有、日志里有、且不经过根应用路由。"""

    import app.main

    legal = "mounted-id-7"
    path = _mount_dummy_sub_app()
    try:
        response = client.get(path + "/ping", headers={"X-Request-ID": legal})
        assert response.status_code == 200
        assert response.headers["x-request-id"] == legal
        assert response.json() == {"status": "ok"}
    finally:
        app.main.app.router.routes = [
            r for r in app.main.app.router.routes if getattr(r, "path", None) != path
        ]

    records = [r for r in _records(log_dir) if r.get("msg") == "t3 mounted sub-app line"]
    assert len(records) == 1
    assert records[0]["request_id"] == legal
    assert records[0]["kb_id"] == "k-sub"


def test_response_body_shape_is_unchanged(client: TestClient) -> None:
    """标识只进响应头与日志字段，不进 body；成功/404 的响应形状保持原样。"""

    health = client.get("/health", headers={"X-Request-ID": "shape-id-9"})
    assert health.json() == {"status": "ok"}
    assert health.headers["x-request-id"] == "shape-id-9"
    assert "shape-id-9" not in health.text, "标识只进响应头，不进 body"

    missing = client.get("/no-such-path")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
    assert "x-request-id" in missing.headers
