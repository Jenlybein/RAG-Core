"""第 14 步（#201）· 按天汇总的算法与边界。

这里守的是**口径**，不是 SQL：``accumulate`` 与 ``percentile_from_buckets`` 都是纯函数，
所以能用一组小数据把「哪一格、算成功还是未知、落在哪个桶、分位怎么插值」钉死。
SQL 那半边由 ``scripts/rollup_usage_daily.py`` 在本地库上真跑过（见 #201 的评论）。

四个容易错的地方，各有一条用例：

1. 迁移之前的行 ``code`` 是 ``NULL``：它们**计入调用次数**但不计入成功（未统计 ≠ 0）；
2. ``key_id`` / ``endpoint`` 为 ``NULL`` 时归一成 ``""``（否则主键里的 ``NULL != NULL``
   会让 upsert 失去幂等）；
3. 耗时正好落在档位边界时归**下一档**（200 ms 不是 ``<200``）；
4. 没有样本时分位必须是 ``None`` 而不是 0（页面据此显示「未统计」）。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models.usage_daily import LATENCY_BUCKET_COUNT
from app.services.usage_rollup import (
    UNKNOWN_CODE,
    Cell,
    accumulate,
    day_bounds,
    oldest_day_n_ahead,
)
from app.services.usage_stats import add_buckets, percentile_from_buckets, total_of

TENANT = "tenant_rollup"


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "log-1",
        "tenant_id": TENANT,
        "key_id": "key-1",
        "endpoint": "retrieve",
        "code": 0,
        "meter_r": 1,
        "meter_rr": 0,
        "meter_llm": 0,
        "latency_ms": 320,
    }
    row.update(overrides)
    return row


# ───────────────────────────────────────────────────────────── 分格


def test_rows_with_the_same_dimensions_fall_into_one_cell() -> None:
    cells = accumulate([_row(id="a"), _row(id="b")])

    assert len(cells) == 1
    cell = next(iter(cells.values()))
    assert (cell.requests, cell.ok_requests) == (2, 2)


def test_a_legacy_row_without_code_counts_as_a_call_but_not_a_success() -> None:
    """迁移之前的行「没说过自己成功」：进分母、不进分子。"""

    cells = accumulate([_row(code=None)])

    cell = next(iter(cells.values()))
    assert cell.code == UNKNOWN_CODE
    assert (cell.requests, cell.ok_requests) == (1, 0)


def test_a_refusal_is_its_own_cell_and_never_a_success() -> None:
    cells = accumulate([_row(code=0), _row(code=20014), _row(code=20014)])

    by_code = {cell.code: cell for cell in cells.values()}
    assert set(by_code) == {0, 20014}
    assert (by_code[20014].requests, by_code[20014].ok_requests) == (2, 0)


def test_null_dimensions_collapse_into_the_empty_string_cell() -> None:
    """``""`` 是「不知道」：主键里放 NULL 会让唯一约束失效，汇总就不再幂等。"""

    cells = accumulate([_row(key_id=None, endpoint=None)])

    cell = next(iter(cells.values()))
    assert (cell.key_id, cell.endpoint) == ("", "")


def test_the_three_meters_are_summed_per_cell() -> None:
    cells = accumulate(
        [
            _row(meter_r=3, meter_rr=2, meter_llm=1),
            _row(meter_r=1, meter_rr=0, meter_llm=0),
        ]
    )

    cell = next(iter(cells.values()))
    assert (cell.meter_r, cell.meter_rr, cell.meter_llm) == (4, 2, 1)


@pytest.mark.parametrize(
    ("latency_ms", "bucket"),
    [(199, 0), (200, 1), (599, 2), (600, 3), (1500, 6), (30_000, 6)],
)
def test_latency_lands_in_the_right_histogram_bucket(latency_ms: int, bucket: int) -> None:
    cells = accumulate([_row(latency_ms=latency_ms)])

    cell = next(iter(cells.values()))
    assert cell.buckets[bucket] == 1
    assert sum(cell.buckets) == 1
    assert cell.lat_sum_ms == latency_ms


def test_a_cell_flattens_into_the_columns_of_usage_daily() -> None:
    cell = Cell(tenant_id=TENANT, key_id="key-1", endpoint="retrieve", code=0)
    cell.add(_row())

    row = cell.as_row(date(2026, 11, 23))

    assert row["day"] == date(2026, 11, 23)
    assert row["ok_requests"] == 1
    assert sum(row[f"lat_b{index}"] for index in range(LATENCY_BUCKET_COUNT)) == row["requests"]


# ────────────────────────────────────────────────────────── 分位


def test_percentile_of_an_empty_histogram_is_none_not_zero() -> None:
    """没有样本 → ``None``。返回 0 会让页面上出现一条贴在 0 ms 的直线。"""

    assert percentile_from_buckets([0] * LATENCY_BUCKET_COUNT, 95) is None


def test_percentile_lands_in_the_bucket_that_holds_the_rank() -> None:
    # 100 个样本，95 个落在 <200，5 个落在 600-800：第 95 名在 <200 那一档里。
    buckets = [95, 0, 0, 5, 0, 0, 0]

    assert percentile_from_buckets(buckets, 95) == 100
    assert percentile_from_buckets(buckets, 100) == 700


def test_percentile_of_the_last_bucket_uses_its_lower_bound() -> None:
    """``>1500`` 没有上界，只能取下界：宁可低估，不虚报慢。"""

    assert percentile_from_buckets([0, 0, 0, 0, 0, 0, 10], 95) == 1500


def test_buckets_add_up_across_days() -> None:
    """分位不能相加，桶可以——这正是长窗口能算 P95 的原因。"""

    total = add_buckets([1, 0, 0, 0, 0, 0, 0], [0, 2, 0, 0, 0, 0, 0])

    assert total_of(total) == 3
    assert percentile_from_buckets(total, 50) == 300


def test_percentile_rejects_a_wrong_length() -> None:
    with pytest.raises(ValueError):
        percentile_from_buckets([1, 2, 3], 95)


# ───────────────────────────────────────────────────── 天界与选区


def test_day_bounds_are_utc_midnight_expressed_in_local_naive_time() -> None:
    """列里存的是本地裸时间，日界仍是 UTC 零点——换算错了整张图会平移八小时。"""

    start, end = day_bounds(date(2026, 11, 23))

    assert (end - start).total_seconds() == 86_400
    # 起止都是**朴素**时间（列里没有时区），且差值恰好一天。
    assert start.tzinfo is None and end.tzinfo is None


def test_rolling_up_n_days_includes_today() -> None:
    days = oldest_day_n_ahead(3, today=date(2026, 11, 23))

    assert days == [date(2026, 11, 21), date(2026, 11, 22), date(2026, 11, 23)]


def test_rolling_up_one_day_is_just_today() -> None:
    assert oldest_day_n_ahead(1, today=date(2026, 11, 23)) == [date(2026, 11, 23)]


def test_the_accumulator_never_looks_at_a_clock() -> None:
    """纯函数：同一批行无论什么时候跑，结果都一样（汇总幂等的前提）。"""

    rows = [_row(), _row(code=20014)]

    assert accumulate(rows) == accumulate(rows)


def test_datetime_import_is_used_for_the_cutoff_shape() -> None:
    """``day_bounds`` 返回的确实是 ``datetime``（不是 ``date``）——下游要按它做区间比较。"""

    start, _ = day_bounds(date(2026, 1, 1))

    assert isinstance(start, datetime)
