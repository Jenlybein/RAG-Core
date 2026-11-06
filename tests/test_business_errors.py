"""T4 · 业务失败统一名字与唯一出口：契约与 ASGI 边界验收。

测试接缝只有一条：ASGI 应用边界（TestClient 发真实请求，只看 HTTP 状态与响应体）。
数据库依赖用 dependency_overrides 替换、仓储/业务模块函数用 monkeypatch 替换，
让同一条接缝离线；错误码枚举的唯一性/分段/全表一致是数据契约断言，
独立来源是 docs/设计决策.md 的权威全表，而不是枚举自身。
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

AUTHORITATIVE_DOC = Path(__file__).resolve().parents[1] / "docs" / "设计决策.md"


def _retrieve_payload(**overrides: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kb_ids": ["kb-missing"],
        "query": "hello",
    }
    payload.update(overrides)
    return payload


def _nested_keys(obj: Any) -> list[str]:
    """递归收集响应体里所有 dict 的键，用来断言没有回显原始输入/上下文。"""

    keys: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.append(key)
            keys.extend(_nested_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_nested_keys(item))
    return keys


def _authoritative_pairs() -> list[tuple[str, int]]:
    """从权威全表读回 (枚举名, 编号)，不把数量写死在测试里。"""

    text = AUTHORITATIVE_DOC.read_text(encoding="utf-8")
    section = text.split("## 错误码全表", 1)[1].split("**HTTP 状态码规则**", 1)[0]
    return [
        (name, int(value))
        for value, name in re.findall(
            r"^\|\s*`(\d+)`\s*\|\s*([A-Z][A-Z_]*)\s*\|", section, flags=re.MULTILINE
        )
    ]


@pytest.fixture
def client():
    """离线 ASGI 客户端：只替换数据库依赖，其余走真实应用与真实处理器。"""

    main = importlib.import_module("app.main")
    session = importlib.import_module("app.db.session")
    main.app.dependency_overrides[session.get_db] = lambda: object()
    from app.core.auth import DevKeyContext, resolve_dev_key
    from app.core.config import settings

    main.app.dependency_overrides[resolve_dev_key] = lambda: DevKeyContext(
        tenant_id=settings.DEV_TENANT_ID
    )
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


# ── 数据契约：错误码词汇 ──


def test_error_codes_match_authoritative_table() -> None:
    from app.core.exceptions import ErrorCode

    assert issubclass(ErrorCode, IntEnum)
    assert [(member.name, int(member.value)) for member in ErrorCode] == _authoritative_pairs()


def test_error_code_values_are_unique() -> None:
    from app.core.exceptions import ErrorCode

    values = [int(member.value) for member in ErrorCode]
    assert len(values) == len(set(values))


def test_error_codes_keep_responsibility_segments() -> None:
    from app.core.exceptions import ErrorCode

    for member in ErrorCode:
        if member.value == 0:
            assert member.name == "SUCCESS"
            continue
        assert (
            20000 <= member.value < 30000
            or 40000 <= member.value < 50000
            or 50000 <= member.value < 60000
        ), f"{member.name} 落在未定案的段位"
    assert not [member for member in ErrorCode if 30000 <= int(member.value) < 40000]


# ── 数据契约：业务异常与统一响应构造器 ──


def test_app_error_defaults_message_from_enum_name() -> None:
    from app.core.exceptions import AppError, ErrorCode

    unauthorized = AppError(ErrorCode.UNAUTHORIZED)
    assert unauthorized.code is ErrorCode.UNAUTHORIZED
    assert unauthorized.msg == "unauthorized"
    assert unauthorized.data is None
    assert str(unauthorized) == "unauthorized"

    assert AppError(ErrorCode.REQUEST_VALIDATION_ERROR).msg == "request validation error"
    assert AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, "custom hint").msg == "custom hint"

    with_data = AppError(ErrorCode.FORBIDDEN, data={"reason": "x"})
    assert with_data.msg == "forbidden"
    assert with_data.data == {"reason": "x"}


def test_fail_and_ok_keep_the_envelope_contract() -> None:
    from app.core.exceptions import ErrorCode
    from app.schemas.common import fail, ok

    assert fail(ErrorCode.UNAUTHORIZED).model_dump() == {
        "code": 20010,
        "msg": "unauthorized",
        "data": None,
    }
    assert ok().model_dump() == {"code": 0, "msg": "success", "data": None}


# ── 行为接缝：ASGI 应用边界 ──


def test_nonexistent_knowledge_base_returns_business_code(client, monkeypatch) -> None:
    repo = importlib.import_module("app.repositories.knowledge_base_repository")
    monkeypatch.setattr(repo, "get_by_id", lambda db, kb_id: None)

    response = client.post("/api/v1/rag/retrieve", json=_retrieve_payload())

    assert response.status_code == 200
    assert response.json() == {
        "code": 20008,
        "msg": "knowledge base not found",
        "data": None,
    }


def test_cross_tenant_and_nonexistent_share_the_same_code(client, monkeypatch) -> None:
    repo = importlib.import_module("app.repositories.knowledge_base_repository")

    monkeypatch.setattr(repo, "get_by_id", lambda db, kb_id: SimpleNamespace(tenant_id="tenant-b"))
    cross_tenant = client.post("/api/v1/rag/retrieve", json=_retrieve_payload())

    monkeypatch.setattr(repo, "get_by_id", lambda db, kb_id: None)
    nonexistent = client.post("/api/v1/rag/retrieve", json=_retrieve_payload())

    assert cross_tenant.status_code == nonexistent.status_code == 200
    assert cross_tenant.json() == nonexistent.json()
    assert cross_tenant.json()["code"] == 20008


def test_document_upload_nonexistent_knowledge_base_uses_the_same_exit(client, monkeypatch) -> None:
    repo = importlib.import_module("app.repositories.knowledge_base_repository")
    monkeypatch.setattr(repo, "get_by_id", lambda db, kb_id: None)

    response = client.post(
        "/api/v1/documents/upload",
        json={
            "kb_id": "kb-missing",
            "title": "demo",
            "content": "text",
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 20008
    assert response.json()["msg"] == "knowledge base not found"


def test_missing_field_returns_validation_code_without_echoing_input(client) -> None:
    payload = _retrieve_payload(kb_ids=["RAW-INPUT-SENTINEL"])
    del payload["query"]

    response = client.post("/api/v1/rag/retrieve", json=payload)

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 20002
    errors = body["data"]["errors"]
    assert errors
    assert any("query" in error["loc"] for error in errors)
    for error in errors:
        assert set(error) == {"loc", "msg", "type"}
    assert "RAW-INPUT-SENTINEL" not in response.text
    assert not {"input", "ctx", "url"} & set(_nested_keys(body))


def test_bytes_body_validation_error_never_becomes_a_server_error(client) -> None:
    """裸 exc.errors() 里的 input 可能是 bytes，直接进 JSONResponse 会崩成 500。"""

    sentinel = b"RAW-BYTES-SENTINEL"
    response = client.post(
        "/api/v1/rag/retrieve",
        content=sentinel,
        headers={"content-type": "text/plain"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 20002
    for error in body["data"]["errors"]:
        assert set(error) == {"loc", "msg", "type"}
    assert "RAW-BYTES-SENTINEL" not in response.text


def test_app_error_optional_data_reaches_the_response() -> None:
    from app.api.error_handlers import install_handlers
    from app.core.exceptions import AppError, ErrorCode

    adapter_app = FastAPI()

    @adapter_app.get("/boom")
    async def boom() -> None:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, data={"hint": "check kb_id"})

    install_handlers(adapter_app)

    with TestClient(adapter_app) as test_client:
        response = test_client.get("/boom")

    assert response.status_code == 200
    assert response.json() == {
        "code": 20008,
        "msg": "knowledge base not found",
        "data": {"hint": "check kb_id"},
    }


def test_success_response_shape_is_unchanged(client, monkeypatch) -> None:
    service = importlib.import_module("app.services.knowledge_base_service")

    async def fake_create_kb(db, *, name, description, tenant_id):
        return {
            "kb_id": "kb-1",
            "name": name,
            "tenant_id": tenant_id,
            "created_at": datetime(2024, 1, 2, 3, 4, 5),
        }

    monkeypatch.setattr(service, "create_kb", fake_create_kb)

    response = client.post(
        "/api/v1/knowledge-bases/create",
        json={"name": "demo"},
    )

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"code", "msg", "data"}
    assert body["code"] == 0
    assert body["msg"] == "success"
    assert body["data"]["kb_id"] == "kb-1"
    assert body["data"]["created_at"] == "2024-01-02T03:04:05"


def test_health_and_unknown_path_are_not_wrapped(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}

    not_found = client.get("/api/v1/does-not-exist")
    assert not_found.status_code == 404
    assert not_found.json() == {"detail": "Not Found"}


def test_fail_envelope_is_json_safe_when_data_contains_bytes() -> None:
    """补充数据可能带 bytes；入口适配器统一过 jsonable_encoder。"""

    from app.core.exceptions import ErrorCode
    from app.schemas.common import fail

    assert jsonable_encoder(fail(ErrorCode.UNAUTHORIZED, data={"raw": b"hello"})) == {
        "code": 20010,
        "msg": "unauthorized",
        "data": {"raw": "hello"},
    }
