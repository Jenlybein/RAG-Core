"""第 13 步 · 端点授权位的端到端验收（ASGI 接缝）。

纯函数门禁在 ``test_endpoint_authorization.py`` 里验过；这里证明它**真的接进了
路由**：把 ``decompose_allowed`` 关掉的请求拿不到 20013 以外的结果，而且
在召回之前就被拒（不烧一次模型、一次嵌入）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

import tests.test_multi_query_union as u
from app.core.auth import DEV_KEY_PLAN, DEV_KEY_POLICY, KeyContext
from app.core.exceptions import ErrorCode


def test_a_denied_decompose_bit_returns_20013_before_any_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decompose 位关掉 → 20013，一次嵌入/模型都不发。"""

    from app.api.deps import get_current_key
    from app.main import app

    denied_policy = replace(DEV_KEY_POLICY, decompose_allowed=False)

    with u._offline_client(monkeypatch) as fake:
        app.dependency_overrides[get_current_key] = lambda: KeyContext(
            tenant_id=u.TENANT_ID,
            policy=denied_policy,
            plan=DEV_KEY_PLAN,
        )
        body = fake["client"].post(
            "/api/v1/rag/decompose",
            json={"kb_ids": [u.KB_A], "query": u.RAW_QUERY},
        ).json()

    assert body["code"] == ErrorCode.FEATURE_NOT_ALLOWED
    assert body["data"] is None
    assert fake["embedding"].queries == [], "被拒的请求不该白烧一次嵌入"


def test_a_denied_rewrite_bit_returns_20013_before_any_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rewrite 位关掉 → 20013，改写模型都不装配。"""

    from app.api.deps import get_current_key
    from app.main import app

    async def _forbidden(_role: str = "default"):
        raise AssertionError("被拒的请求不该装配大模型")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _forbidden)
    denied_policy = replace(DEV_KEY_POLICY, rewrite_allowed=False)

    with u._offline_client(monkeypatch) as fake:
        app.dependency_overrides[get_current_key] = lambda: KeyContext(
            tenant_id=u.TENANT_ID,
            policy=denied_policy,
            plan=DEV_KEY_PLAN,
        )
        body = fake["client"].post(
            "/api/v1/rag/rewrite",
            json={"kb_ids": [u.KB_A], "query": u.RAW_QUERY},
        ).json()

    assert body["code"] == ErrorCode.FEATURE_NOT_ALLOWED


def test_retrieve_stays_available_when_processing_bits_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """基线能力不受加工位影响：rewrite/decompose 都关，direct 仍可用。"""

    from app.api.deps import get_current_key
    from app.main import app

    lean_policy = replace(
        DEV_KEY_POLICY, rewrite_allowed=False, decompose_allowed=False
    )

    with u._offline_client(monkeypatch) as fake:
        app.dependency_overrides[get_current_key] = lambda: KeyContext(
            tenant_id=u.TENANT_ID,
            policy=lean_policy,
            plan=DEV_KEY_PLAN,
        )
        body = u._response(fake["client"]).json()

    assert body["code"] == ErrorCode.SUCCESS
