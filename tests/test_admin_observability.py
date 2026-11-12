"""管理端观测总览的口径守卫。

这里不测 SQL（那要靠真库），只测**这台看板容易骗人的三个地方**：

1. **取不到不等于 0**：观测栈不可用时 `stages` 必须回 `source=none` 与空列表，而不是
   一屏 0 毫秒的瀑布图——那会被读成「检索变快了」。
2. **分位数不能拍 0**：空样本回 `None`；`degraded_rate` 在分母为 0 时回 `None`。
3. **密钥不出服务端**：`stack` 只回布尔与地址。

聚合的 SQL 形状（分位数下推、按小时分桶）由真库保证，这里用一个假的「读端」把口径与
降级路径钉死。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from app.core.config import settings
from app.services import admin_observability


@dataclass
class _Observation:
    """观测栈里一条 span 的最小形状（SDK 对象在我们的读端只用到这两个属性）。"""

    name: str
    metadata: dict[str, Any]


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-x")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-x")


def _unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")


def test_stages_are_none_when_the_stack_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unconfigured(monkeypatch)

    result = admin_observability.stage_stats(hours=24)

    assert result["source"] == admin_observability.STAGE_SOURCE_NONE
    assert result["reason"] == "stack_not_configured"
    # 关键：**空列表**而不是六段全 0。0 毫秒会被读成「检索变快了」。
    assert result["stages"] == []


def test_stages_degrade_to_none_when_the_stack_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)

    def _boom(since: datetime) -> list[Any]:
        raise RuntimeError("connect failed")

    result = admin_observability.stage_stats(hours=24, reader=_boom)

    assert result["source"] == admin_observability.STAGE_SOURCE_NONE
    assert result["reason"] == "stack_unreachable"
    assert result["stages"] == []


def test_stages_aggregate_percentiles_and_share(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    observations = [
        _Observation("vector_recall", {"duration_ms": 10.0}),
        _Observation("vector_recall", {"duration_ms": 30.0}),
        _Observation("keyword_recall", {"duration_ms": 60.0}),
        # 不认识的段名（别的 instrumentation 挂上来的）必须丢掉：
        # 六段是一份契约，混进别的段会让「占比」失去意义。
        _Observation("some_other_span", {"duration_ms": 999.0}),
    ]

    result = admin_observability.stage_stats(hours=24, reader=lambda since: observations)

    assert result["source"] == admin_observability.STAGE_SOURCE_LANGFUSE
    by_name = {stage["name"]: stage for stage in result["stages"]}
    vector = by_name["vector_recall"]
    assert vector["samples"] == 2
    assert vector["p50"] == pytest.approx(20.0)
    assert vector["p95"] == pytest.approx(29.0)
    # 10+30 占 (10+30+60) 的 40%
    assert vector["share"] == pytest.approx(0.4, abs=0.01)
    # 没有样本的段如实回 None，不补 0
    assert by_name["fusion"]["p50"] is None
    assert by_name["fusion"]["samples"] == 0


def test_an_observation_without_duration_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 duration_ms 的 span 不能当成 0 毫秒计入分位数。"""

    _configured(monkeypatch)
    observations = [
        _Observation("fusion", {}),
        _Observation("fusion", {"duration_ms": 5.0}),
    ]

    result = admin_observability.stage_stats(hours=24, reader=lambda since: observations)

    fusion = next(stage for stage in result["stages"] if stage["name"] == "fusion")
    assert fusion["samples"] == 1
    assert fusion["p50"] == pytest.approx(5.0)


def test_stack_reports_flags_but_never_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_URL", "http://localhost:3000")

    status = admin_observability.stack_status()

    assert status["sink"] == "langfuse"
    assert status["has_keys"] is True
    assert status["stack_layer_available"] is True
    # 对外地址（浏览器能解析的那个），不是容器里的服务名
    assert status["langfuse_host"] == "http://localhost:3000"
    # 私钥一个字都不许出现在返回值里
    assert "sk-x" not in str(status)
    assert "pk-x" not in str(status)


def test_stack_says_no_layer_when_sink_is_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-x")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-x")

    status = admin_observability.stack_status()

    # 有凭据但去向不是 langfuse：栈里没有数据，于是这一层不可用。
    assert status["stack_layer_available"] is False
    assert status["langfuse_host"] is None


def test_illegal_window_falls_back_to_24_hours() -> None:
    """看板不该因为一个 query 参数报错：非法窗口收敛到 24 小时。"""

    since = admin_observability.window_start(999)
    now = datetime.now(timezone.utc)

    assert 23 < (now - since).total_seconds() / 3600 < 25


def test_ratings_average_is_none_when_nothing_was_rated() -> None:
    """没有评分时平均分是 `None`，不是 0：0 分会读成「大家都很不满意」。"""

    assert admin_observability._percentile([], 0.5) is None
