"""第 14 步（#202）· 用量看板的**口径守卫**。

不测 SQL 的脾气（那要靠真库），只测那些「一眼看不出来、但会让看板撒谎」的地方：

1. **窗口对齐**：1 小时是 6 个 10 分钟桶、1 天是 24 个 1 小时桶、7/30 天是日历天；
   非法的 `?window=` 收敛到 1 天而不是 400（看板不该因为一个参数报错）。
2. **走哪张表**：`1h/1d` 读明细（P95 精确），`7d/30d` 读日汇总（P95 来自直方图，要标出来）。
3. **空窗口不是 0**：没样本时 `success_rate` / `p50_ms` / `p95_ms` 都是 `None`；桶照补齐，
   但数值是 0、不是 `None`（「没有请求」和「不知道」是两件事）。
4. **租户隔离**：租户那条路的每条 SQL 都带 `tenant_id`。
5. **三段式**：`scope` 只影响「量」，`rates_meaningful` 说明降级率/空结果率这一档有没有意义。

假会话只做一件事：按顺序把预先准备好的结果发回去，并把收到的语句记下来。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services import admin_observability, console_usage
from app.services.usage_rollup import day_bounds


class _Row(dict):
    """``Row`` 的最小替身：服务层只用到 ``._mapping``。"""

    @property
    def _mapping(self) -> dict[str, Any]:
        return self


class _Result:
    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        one: Any = None,
        scalar: Any = 0,
        scalar_one: Any | None = None,
    ) -> None:
        self._rows = rows or []
        self._one = one if one is not None else _Row()
        self._scalar = scalar
        self._scalar_one = scalar if scalar_one is None else scalar_one

    def all(self) -> list[Any]:
        return self._rows

    def one(self) -> Any:
        return self._one

    def scalars(self) -> "_Result":
        return self

    def scalar_one(self) -> Any:
        return self._scalar_one

    def scalar_one_or_none(self) -> Any:
        """聚合查询的收尾：第四本账那条用的就是它（空结果按 0 读）。"""

        return self._scalar


class _StubSession:
    """按顺序发结果的假会话；同时记下每条语句（用来查「有没有带租户」）。"""

    def __init__(self, *results: _Result) -> None:
        self._queue = list(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if not self._queue:
            raise AssertionError("假会话没料了：服务层比预期多查了一次")
        return self._queue.pop(0)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Any) -> list[str]:
    return [str(value) for value in statement.compile(dialect=postgresql.dialect()).params.values()]


# ─────────────────────────────────────────────────────────── 窗口


def test_one_hour_is_six_ten_minute_buckets_anchored_on_the_clock() -> None:
    now = datetime(2026, 11, 24, 14, 37, 21)

    window = console_usage.parse_window("1h", now=now)

    assert window.count == 6
    assert window.bucket_seconds == 600
    # 锚点对齐到 10 分钟：不对齐的话连刷两次，柱子会左右抖，看起来像「数据在变」。
    assert window.since == datetime(2026, 11, 24, 13, 40)
    assert window.until == datetime(2026, 11, 24, 14, 40)


def test_one_day_is_twenty_four_hourly_buckets_ending_with_the_current_hour() -> None:
    window = console_usage.parse_window("1d", now=datetime(2026, 11, 24, 14, 37))

    assert window.count == 24
    assert window.bucket_seconds == 3600
    assert window.since == datetime(2026, 11, 23, 15, 0)
    assert window.until == datetime(2026, 11, 24, 15, 0)


def test_seven_and_thirty_days_use_calendar_days_from_the_daily_table() -> None:
    for name, days in (("7d", 7), ("30d", 30)):
        window = console_usage.parse_window(name)

        assert window.uses_daily is True
        assert window.count == days
        assert window.bucket_seconds == 86_400


def test_an_unknown_window_falls_back_to_one_day_instead_of_failing() -> None:
    assert console_usage.parse_window("last-quarter").name == "1d"
    assert console_usage.parse_window(None).name == "1d"


def test_the_daily_window_starts_at_a_utc_midnight_expressed_locally() -> None:
    """7 / 30 天是**日历天**，起止都落在 UTC 零点（换算成列里那个本地裸时间）。"""

    window = console_usage.parse_window("7d")

    assert window.since.time() == window.until.time(), "两端都是同一个 UTC 零点"
    assert (window.until.date() - window.since.date()).days == 7
    assert window.since == day_bounds(window.since.date())[0]


# ─────────────────────────────────────────────────────── 序列与合计


@pytest.mark.asyncio
async def test_a_short_window_reads_the_detail_table_and_pads_empty_buckets() -> None:
    empty_totals = _Row(
        requests=0,
        ok=0,
        failed=0,
        unknown=0,
        unknown_code=0,
        p50=None,
        p95=None,
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
        meter_chunk=0,
    )
    session = _StubSession(
        _Result(rows=[]),
        _Result(one=empty_totals),
        # 第四本账（#222）是单独两条查询：合计一条 + 按桶一条（曲线图要四本并列）。
        _Result(scalar=0),
        _Result(rows=[]),
    )

    data = await console_usage.series(session, window=console_usage.parse_window("1d"))

    assert data["source"] == "events"
    assert data["p95_source"] == "exact"
    assert len(data["buckets"]) == 24, "空窗口也要补齐 24 桶，否则图上会少一截"
    assert data["totals"]["success_rate"] is None, "没有样本时不是 0%"
    assert data["totals"]["p95_ms"] is None


@pytest.mark.asyncio
async def test_a_long_window_reads_the_daily_table_and_labels_the_histogram() -> None:
    daily_totals = _Row(
        requests=10,
        ok=9,
        unknown=1,
        meter_r=10,
        meter_rr=0,
        meter_llm=0,
        meter_chunk=0,
        **{f"lat_b{index}": (2 if index == 1 else 0) for index in range(7)},
    )
    # 桶 1 有 2 个样本 → 用第 1 档的中点回答 P95；这里只验「标注」与「不是平均值」。
    session = _StubSession(
        _Result(rows=[]),
        _Result(one=daily_totals),
        _Result(scalar=0),
        _Result(scalar=0),
    )

    data = await console_usage.series(session, window=console_usage.parse_window("30d"))

    assert data["source"] == "daily"
    assert data["p95_source"] == "histogram", "长窗的分位来自桶，页面要据此标「近似」"
    assert data["totals"]["requests"] == 10
    assert "usage_daily" in _sql(session.statements[0])


@pytest.mark.asyncio
async def test_every_tenant_scoped_query_carries_the_tenant() -> None:
    """租户那条路漏一次 ``tenant_id`` 就是跨租户读——所以每条语句都查。"""

    session = _StubSession(
        _Result(rows=[]),
        _Result(
            one=_Row(
                requests=0,
                ok=0,
                p50=None,
                p95=None,
                unknown=0,
                meter_r=0,
                meter_rr=0,
                meter_llm=0,
                meter_chunk=0,
                unknown_code=0,
            )
        ),
        # 第四本账（#222）：两条都必须带租户（跨租户的用量合计是一条不该存在的查询）。
        _Result(scalar=0),
        _Result(rows=[]),
    )

    await console_usage.series(
        session, window=console_usage.parse_window("1d"), tenant_id="tenant-a"
    )

    for statement in session.statements:
        assert "tenant_id" in _sql(statement), "有一条查询没带租户"
        assert "tenant-a" in _params(statement)


# ─────────────────────────────────────────────────── 按 Key 拆分


@pytest.mark.asyncio
async def test_a_deleted_key_keeps_its_history_row_and_is_marked_deleted() -> None:
    """删掉 Key 不删账：那一行要留下，并标成「已删除」，而不是从表里消失。"""

    usage_rows = [
        _Row(key_id="key-live", requests=5, ok=5, unknown=0),
        _Row(key_id="key-gone", requests=2, ok=0, unknown=0),
    ]
    live_key = type("K", (), {"id": "key-live", "label": "prod", "key_tail": "abcd"})()
    session = _StubSession(_Result(rows=usage_rows), _Result(rows=[live_key]))

    rows = await console_usage.keys_of_tenant(
        session, tenant_id="tenant-a", window=console_usage.parse_window("1d")
    )

    by_key = {row["key_id"]: row for row in rows}
    assert by_key["key-live"]["deleted"] is False
    assert by_key["key-live"]["masked"].endswith("abcd")
    assert by_key["key-gone"]["deleted"] is True
    assert by_key["key-gone"]["label"] is None
    assert rows[0]["key_id"] == "key-live", "按调用量倒序"


@pytest.mark.asyncio
async def test_success_rate_ignores_rows_whose_outcome_was_never_recorded() -> None:
    """迁移之前的行没有码：它们进「调用次数」，不进成功率的分母。

    整窗都是这种行时成功率必须是 ``None``（页面显示「未统计」）——算成 0% 会被读成
    「全挂了」，而真相是「那时候还没记账」。
    """

    all_legacy = _Row(
        requests=3,
        ok=0,
        unknown=3,
        p50=100.0,
        p95=200.0,
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
        meter_chunk=0,
    )
    session = _StubSession(
        _Result(rows=[]),
        _Result(one=all_legacy),
        # 第四本账（#222）：合计一条 + 按桶一条。
        _Result(scalar=0),
        _Result(rows=[]),
    )

    data = await console_usage.series(session, window=console_usage.parse_window("1d"))

    assert data["totals"]["requests"] == 3
    assert data["totals"]["unknown_code"] == 3
    assert data["totals"]["success_rate"] is None


@pytest.mark.asyncio
async def test_success_rate_denominator_is_the_known_ones_only() -> None:
    mixed = _Row(
        requests=5,
        ok=2,
        unknown=1,
        p50=100.0,
        p95=200.0,
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
        meter_chunk=0,
    )
    session = _StubSession(
        _Result(rows=[]),
        _Result(one=mixed),
        # 第四本账（#222）：索引行不进请求统计，因此单独读一次合计……
        _Result(scalar=0),
        # ……再读一次**按桶**的量（曲线图要四本并列，见 `_chunk_buckets`）。
        _Result(rows=[]),
    )

    data = await console_usage.series(session, window=console_usage.parse_window("1d"))

    # 2 成功 / （5 - 1 未统计）= 50%，而不是 2/5 = 40%。
    assert data["totals"]["success_rate"] == pytest.approx(0.5)


# ─────────────────────────────────────────────────────── 三段式


@pytest.mark.asyncio
async def test_rates_are_only_meaningful_in_the_success_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _totals(_db: Any, *, since: datetime, scope: str = "all") -> dict[str, Any]:
        captured["scope"] = scope
        return {
            "total": 3,
            "latency": {"p50": 1.0, "p95": 2.0, "worst": 2.0},
            "degraded": 0,
            "empty": 0,
        }

    async def _ratings(_db: Any, *, since: datetime) -> dict[int, int]:
        return {}

    async def _by_tenant(
        _db: Any, *, since: datetime, limit: int = 10, scope: str = "all"
    ) -> list[Any]:
        return []

    async def _hourly(_db: Any, *, since: datetime, scope: str = "all") -> list[Any]:
        return []

    monkeypatch.setattr(admin_observability.observability_repository, "totals", _totals)
    monkeypatch.setattr(admin_observability.observability_repository, "rating_histogram", _ratings)
    monkeypatch.setattr(admin_observability.observability_repository, "by_tenant", _by_tenant)
    monkeypatch.setattr(admin_observability.observability_repository, "hourly", _hourly)

    failed = await admin_observability.overview(None, hours=24, scope="failed")
    ok = await admin_observability.overview(None, hours=24, scope="ok")

    assert captured["scope"] == "ok", "最后一次调用把 scope 透传下去了"
    assert failed["scope"] == "failed"
    assert failed["rates_meaningful"] is False, "被拒的请求既不降级也不是空结果"
    assert ok["rates_meaningful"] is True


def test_a_bogus_scope_on_the_overview_is_reported_as_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收敛而不是 400：看板不该因为一个 query 参数整页报错。"""

    monkeypatch.setattr(admin_observability.observability_repository, "totals", _totals_empty)
    monkeypatch.setattr(
        admin_observability.observability_repository, "rating_histogram", _ratings_empty
    )
    monkeypatch.setattr(admin_observability.observability_repository, "by_tenant", _list_empty)
    monkeypatch.setattr(admin_observability.observability_repository, "hourly", _list_empty)

    result = asyncio.run(admin_observability.overview(None, hours=24, scope="whatever"))

    assert result["scope"] == "all"
    assert result["rates_meaningful"] is False


async def _totals_empty(_db: Any, *, since: datetime, scope: str = "all") -> dict[str, Any]:
    return {
        "total": 0,
        "latency": {"p50": None, "p95": None, "worst": None},
        "degraded": 0,
        "empty": 0,
    }


async def _ratings_empty(_db: Any, *, since: datetime) -> dict[int, int]:
    return {}


async def _list_empty(_db: Any, **_kwargs: Any) -> list[Any]:
    return []


# ─────────────────────────────────────────────────────────── 告警


@pytest.mark.asyncio
async def test_an_empty_site_window_says_so_instead_of_showing_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _count(_db: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(console_usage, "_count_code", _count)

    found = await console_usage.alerts(
        None,
        window=console_usage.parse_window("1d"),
        site={"totals": {"requests": 0, "success_rate": None}},
    )

    assert [item["kind"] for item in found] == ["no_traffic"]


@pytest.mark.asyncio
async def test_quota_refusals_become_an_error_level_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _count(_db: Any, **_kwargs: Any) -> int:
        return 7

    async def _previous(_db: Any, **_kwargs: Any) -> None:
        return None

    async def _quota(_db: Any) -> list[Any]:
        return []

    monkeypatch.setattr(console_usage, "_count_code", _count)
    monkeypatch.setattr(console_usage, "_previous_window_rate", _previous)
    monkeypatch.setattr(console_usage, "_today_quota_usage", _quota)

    found = await console_usage.alerts(
        None,
        window=console_usage.parse_window("1d"),
        site={"totals": {"requests": 100, "success_rate": 0.9}},
    )

    assert found[0]["kind"] == "quota_full"
    assert found[0]["level"] == "error"
    assert "7" in found[0]["message"]


def test_nan_percentiles_never_reach_the_page_as_zero() -> None:
    assert console_usage._as_float(float("nan")) is None
    assert console_usage._as_float(None) is None
    assert console_usage._as_float(12.5) == 12.5


@pytest.mark.asyncio
async def test_the_tenant_ranking_also_ignores_unknown_outcomes() -> None:
    """榜上的成功率与序列里的口径必须一致：整租户都是老数据时显示「未统计」，不是 0%。"""

    rows = [
        _Row(
            tenant_id="tenant-old",
            requests=4,
            ok=0,
            unknown=4,
            **{f"lat_b{index}": 0 for index in range(7)},
        ),
        _Row(
            tenant_id="tenant-real",
            requests=4,
            ok=3,
            unknown=0,
            **{f"lat_b{index}": (4 if index == 2 else 0) for index in range(7)},
        ),
    ]
    session = _StubSession(_Result(rows=rows))

    ranked = await console_usage.tenants(session, window=console_usage.parse_window("30d"))

    by_tenant = {item["tenant_id"]: item for item in ranked}
    assert by_tenant["tenant-old"]["success_rate"] is None
    assert by_tenant["tenant-real"]["success_rate"] == pytest.approx(0.75)


# ───────────────────────────────────────────── 三段式的过滤条件


def test_scope_conditions_filter_on_the_business_code() -> None:
    """「看板收下了 scope 却忘了过滤」是这一类改动最容易犯的错：参数在、判定不在。

    这里直接编译那几条条件，把「只看成功 = code 0」「只看失败 = code 非 0」钉住；
    ``all`` 必须是**空列表**（而不是恒真条件），否则它会被拼进每条 SQL 白跑一遍。
    """

    from app.repositories.observability_repository import scope_conditions

    dialect = postgresql.dialect()

    def _sql(scope: str) -> str:
        clauses = scope_conditions(scope)
        assert len(clauses) <= 1
        if not clauses:
            return ""
        return str(clauses[0].compile(dialect=dialect, compile_kwargs={"literal_binds": True}))

    assert scope_conditions("all") == []
    assert "usage_events.code = 0" in _sql("ok")
    assert "usage_events.code != 0" in _sql("failed")


@pytest.mark.asyncio
async def test_failed_counts_only_the_known_outcomes() -> None:
    """「失败」不含迁移前那些没有码的行——它们既不是成功也不是失败。

    这条与成功率的分母是同一个纪律（ADR-0033 §3）。算错的形态很具体：页面上
    「失败 N 次」里凭空长出一批老数据，读者会去查一批根本不存在的故障。
    """

    mixed = _Row(
        requests=5,
        ok=2,
        unknown=1,
        p50=100.0,
        p95=200.0,
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
        meter_chunk=0,
    )
    session = _StubSession(
        _Result(rows=[]),
        _Result(one=mixed),
        # 第四本账（#222）：索引行不进请求统计，因此单独读一次合计……
        _Result(scalar=0),
        # ……再读一次**按桶**的量（曲线图要四本并列，见 `_chunk_buckets`）。
        _Result(rows=[]),
    )

    data = await console_usage.series(session, window=console_usage.parse_window("1d"))

    assert data["totals"]["requests"] == 5
    assert data["totals"]["failed"] == 2, "5 次里 2 成功 1 未统计 → 只有 2 次是失败"
    assert data["totals"]["unknown_code"] == 1


@pytest.mark.asyncio
async def test_a_window_of_purely_legacy_rows_reports_no_failures() -> None:
    """整窗都是迁移前的行：失败 0 次、成功率「未统计」，而不是「全挂了」。"""

    legacy = _Row(
        requests=4,
        ok=0,
        unknown=4,
        p50=None,
        p95=None,
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
        meter_chunk=0,
    )
    session = _StubSession(
        _Result(rows=[]),
        _Result(one=legacy),
        # 第四本账（#222）：合计一条 + 按桶一条（曲线图四本并列要后者）。
        _Result(scalar=0),
        _Result(rows=[]),
    )

    data = await console_usage.series(session, window=console_usage.parse_window("1d"))

    assert data["totals"]["failed"] == 0
    assert data["totals"]["success_rate"] is None
    assert data["totals"]["unknown_code"] == 4
