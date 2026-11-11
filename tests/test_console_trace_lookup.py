"""控制面「按 trace_id 反查 log_id」的离线测试（#106）。

测法是**直接调端点函数**并 monkeypatch 仓储：这条端点的逻辑只有三件事——形状校验、
按租户取行、跨租户与不存在同一答复；SQL 本身由真库跑的实跑路径验证，不在这里假装测过。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.api.routes import console_traces


def _identity(tenant_id: str = "tenant_demo") -> Any:
    return SimpleNamespace(tenant_id=tenant_id)


def _row(log_id: str) -> Any:
    return SimpleNamespace(id=log_id)


def test_a_known_trace_resolves_to_its_log_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def _fake(db: Any, trace_id: str, *, tenant_id: str) -> Any:
        seen.append({"trace_id": trace_id, "tenant_id": tenant_id})
        return _row("log-1")

    monkeypatch.setattr(console_traces.retrieval_trace_repository, "get_by_trace_id_for_tenant", _fake)

    body = console_traces.retrieval_trace_by_trace_id(
        trace_id="a" * 32, identity=_identity(), db=object()
    )

    assert body.data["log_id"] == "log-1"
    assert seen == [{"trace_id": "a" * 32, "tenant_id": "tenant_demo"}]


def test_an_unknown_or_other_tenants_trace_resolves_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨租户与不存在**同一个答复**——这条口径与瀑布、反馈完全一致。"""

    monkeypatch.setattr(
        console_traces.retrieval_trace_repository,
        "get_by_trace_id_for_tenant",
        lambda db, trace_id, *, tenant_id: None,
    )

    body = console_traces.retrieval_trace_by_trace_id(trace_id="b" * 32, identity=_identity(), db=object())

    assert body.data["log_id"] is None
    assert body.data["trace_id"] == "b" * 32


@pytest.mark.parametrize("trace_id", ["../../etc/passwd", "abc", "a" * 33, ""])
def test_an_illegal_shape_never_reaches_the_database(
    trace_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """形状不对就是「不存在」，而且**不去查库**：不把任意一串当查询打进去。"""

    called: list[str] = []
    monkeypatch.setattr(
        console_traces.retrieval_trace_repository,
        "get_by_trace_id_for_tenant",
        lambda db, value, *, tenant_id: called.append(value),
    )

    body = console_traces.retrieval_trace_by_trace_id(trace_id=trace_id, identity=_identity(), db=object())

    assert body.data["log_id"] is None
    assert called == [], "非法形状不该触发查询"


def test_uppercase_trace_id_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """``trace_id`` 是十六进制串：大小写不该决定「查不查得到」。"""

    seen: list[str] = []
    monkeypatch.setattr(
        console_traces.retrieval_trace_repository,
        "get_by_trace_id_for_tenant",
        lambda db, value, *, tenant_id: seen.append(value) or _row("log-2"),
    )

    body = console_traces.retrieval_trace_by_trace_id(trace_id="A" * 32, identity=_identity(), db=object())

    assert body.data["trace_id"] == "a" * 32
    assert seen == ["a" * 32]
