"""S0 压测入口（#123）：只读、离线、可复算。

被测的是**脚本的外部行为**，不是它的内部结构：

* 延迟口径：P50 / P95 用**最近秩法**（与 `scripts/eval_gate.py` 同一把尺子，不插值）；
* 「主循环同步 DB 占比」：从会话替身的插桩读数算出来，并且**只有主循环线程上**的调用
  算进分子——执行池线程上的调用不算（那正是第 10 步买来的东西）；
* 报告：三档并发（4 / 16 / 64）的 P50 / P95 / 吞吐 / 错误率、占比与口径、第 10 步既有
  读数（P95 1177.9 ms）对照，全部出现在 markdown 里；
* 整条链路离线：不连真库、不连真 Redis、不写任何库（会话替身 + 既有注入缝）。

接缝只有两个：`scripts/_loadtest_core.py` 的纯函数，和 `scripts/loadtest_baseline.py`
的 CLI（`main(argv)` → 退出码 + `--json-out` + 打印出来的报告）。压测怎么驱动事件循环
（ASGI 进程内、替身装配）属于实现，不在断言范围内。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from scripts._loadtest_core import (
    CONCURRENCY_LEVELS,
    DbShare,
    LevelResult,
    SyncDbLedger,
    percentile_nearest_rank,
    render_report,
    summarize_latency,
)


# ───────────────────────────────────────────────────── 延迟口径：最近秩法


def test_percentile_uses_nearest_rank_without_interpolation() -> None:
    """P50 / P95 取升序第 ⌈p/100 × N⌉ 个：100 个 10..1000 → 500 / 950。

    与 `scripts/eval_gate.py` 同一口径（`tests/test_eval_gate.py` 里那条用例算的是同一
    组数）。插值会造出一个从来没发生过的耗时，所以这里连「半个位置」都不许出现。
    """

    samples = [float(value) for value in range(10, 1001, 10)]

    assert percentile_nearest_rank(samples, 50) == 500.0
    assert percentile_nearest_rank(samples, 95) == 950.0


def test_percentile_of_127_samples_matches_the_step10_report() -> None:
    """N=127 时 P95 是第 121 个——与第 10 步那份报告的口径逐字相同。"""

    samples = [float(value) for value in range(1, 128)]

    assert percentile_nearest_rank(samples, 95) == 121.0
    assert percentile_nearest_rank(samples, 50) == 64.0


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile_nearest_rank([], 95)
    with pytest.raises(ValueError):
        percentile_nearest_rank([1.0], 0)
    with pytest.raises(ValueError):
        percentile_nearest_rank([1.0], 101)


def test_summarize_reports_count_p50_p95_mean_and_max() -> None:
    """一份读数只算一次：P95 与 P50 必须来自同一份样本。"""

    stats = summarize_latency([10.0, 20.0, 30.0, 40.0])

    assert stats["count"] == 4
    assert stats["p50_ms"] == 20.0  # ⌈0.5×4⌉ = 2
    assert stats["p95_ms"] == 40.0  # ⌈0.95×4⌉ = 4
    assert stats["mean_ms"] == 25.0
    assert stats["max_ms"] == 40.0


# ─────────────────────────────────────────── 主循环同步 DB 占比（插桩口径）


def test_ledger_counts_only_main_loop_calls_in_the_share() -> None:
    """分子只算主循环线程上的调用；执行池线程上的调用单独记、不进占比。"""

    ledger = SyncDbLedger(main_thread="MainThread")
    ledger.record("execute", 1.0)  # 可见性校验（主循环）
    ledger.record("execute", 2.0)  # 折叠回查（主循环）
    ledger.record("execute", 10.0, thread="retrieval-pool-1")  # 向量查询（执行池线程）

    share = ledger.share(total_request_ms=100.0)

    assert isinstance(share, DbShare)
    assert share.main_loop_calls == 2
    assert share.main_loop_ms == pytest.approx(3.0)
    assert share.off_loop_calls == 1
    assert share.off_loop_ms == pytest.approx(10.0)
    assert share.share == pytest.approx(0.03)
    assert share.by_method == {"execute": pytest.approx(3.0)}


def test_ledger_keeps_method_breakdown_and_resets_between_levels() -> None:
    """按方法分解（execute / commit…），且每一档并发重新记——不把上一档的读数带进来。"""

    ledger = SyncDbLedger(main_thread="MainThread")
    ledger.record("execute", 1.5)
    ledger.record("commit", 0.5)
    ledger.record("add", 0.0)

    share = ledger.share(total_request_ms=10.0)
    assert share.by_method["execute"] == pytest.approx(1.5)
    assert share.by_method["commit"] == pytest.approx(0.5)

    ledger.reset()
    assert ledger.calls == []
    assert ledger.share(total_request_ms=10.0).share == 0.0


def test_share_is_zero_instead_of_dividing_by_zero() -> None:
    """没有测到请求时占比是 0，而不是 ZeroDivisionError。"""

    assert SyncDbLedger(main_thread="MainThread").share(total_request_ms=0.0).share == 0.0


# ───────────────────────────────────────────────────────── 报告：可复算的读数


def _level(concurrency: int, *, p95: float = 120.0, share: float = 0.25) -> LevelResult:
    return LevelResult(
        concurrency=concurrency,
        requests=160,
        ok=158,
        failed=2,
        degraded=3,
        throughput_rps=64.0,
        wall_ms=2500.0,
        latency={"count": 160, "p50_ms": 40.0, "p95_ms": p95, "mean_ms": 55.0, "max_ms": 900.0},
        db=DbShare(
            main_loop_calls=640,
            main_loop_ms=640.0,
            off_loop_calls=160,
            off_loop_ms=1920.0,
            total_request_ms=8800.0,
            by_method={"execute": 480.0, "commit": 160.0},
            share=share,
        ),
        cross_check={"serial_ms_per_request": 16.0, "loop_busy_share": 0.536},
        db_call_ms=2.0,
    )


def _params() -> dict[str, Any]:
    return {
        "levels": [4, 16, 64],
        "requests": 160,
        "db_call_ms": 2.0,
        "embed_ms": 18.0,
        "vector_ms": 12.0,
        "keyword_ms": 15.0,
        "pool_size": 4,
        "queue_budget_ms": 2000,
    }


def test_report_has_the_three_concurrency_levels_and_every_metric() -> None:
    """报告必须让「4 / 16 / 64 并发下的 P50 / P95 / 吞吐 / 错误率」一眼可读。"""

    report = render_report(
        levels=[_level(4), _level(16), _level(64)],
        params=_params(),
        reference={"p95_ms": 1177.9, "source": "docs/架构.md「评测」"},
    )

    assert CONCURRENCY_LEVELS == (4, 16, 64)
    assert "| 并发 |" in report
    for column in ("P50", "P95", "吞吐", "错误率"):
        assert column in report
    for concurrency in (4, 16, 64):
        assert f"| {concurrency} |" in report


def test_report_states_the_db_share_is_an_approximation_and_how_it_is_computed() -> None:
    """占比是近似——报告里必须自己说清「怎么算的」，而不是给一个孤零零的百分数。"""

    report = render_report(levels=[_level(4)], params=_params(), reference={"p95_ms": 1177.9})

    assert "主循环同步 DB" in report
    assert "近似" in report
    # 口径三件套：分子、分母、以及「执行池线程上的调用不算」这条边界
    assert "主循环线程" in report
    assert "执行池" in report
    assert "分母" in report


def test_report_records_the_step10_reading_as_a_reference_point() -> None:
    """第 10 步既有读数（单请求 P95 1177.9 ms）必须出现在报告里，且标明不可直接相比。"""

    report = render_report(
        levels=[_level(4)],
        params=_params(),
        reference={"p95_ms": 1177.9, "source": "docs/架构.md「评测」"},
    )

    assert "1177.9" in report
    assert "第 10 步" in report


def test_report_carries_the_reproduce_command_and_the_nearest_rank_rule() -> None:
    """任何人按命令可复算；口径写在报告里而不是靠口口相传。"""

    report = render_report(levels=[_level(4)], params=_params(), reference={"p95_ms": 1177.9})

    assert "uv run python scripts/loadtest_baseline.py" in report
    assert "最近秩" in report
    assert "不插值" in report


# ─────────────────────────────────────────────── CLI：整条链路离线跑通（真跑）


def _run_cli(tmp_path: Path, *extra: str) -> tuple[int, dict[str, Any], str]:
    """跑一次真 CLI：两档并发、每档 8 个请求——够短，但走的是同一条链路。"""

    from scripts import loadtest_baseline

    json_out = tmp_path / "results.json"
    code = loadtest_baseline.main(
        [
            "--levels",
            "2,4",
            "--requests",
            "8",
            "--warmup",
            "2",
            "--db-call-ms",
            "0.5",
            "--embed-ms",
            "2",
            "--vector-ms",
            "2",
            "--keyword-ms",
            "2",
            *extra,
            "--json-out",
            str(json_out),
        ]
    )
    assert code == 0
    return code, json.loads(json_out.read_text(encoding="utf-8")), json_out.read_text(
        encoding="utf-8"
    )


def test_cli_runs_the_whole_chain_offline_and_reports_zero_errors(tmp_path: Path) -> None:
    """ASGI 进程内跑真应用 + 离线替身：两档并发、每个请求都成功、占比有读数。"""

    _code, payload, _raw = _run_cli(tmp_path)

    assert [level["concurrency"] for level in payload["levels"]] == [2, 4]
    for level in payload["levels"]:
        assert level["requests"] == 8
        assert level["ok"] == 8
        assert level["failed"] == 0
        assert level["error_rate"] == 0.0
        assert level["latency"]["count"] == 8
        # 主循环上确实有同步 DB 调用被记到（可见性 / 折叠 / 归属索引写入）
        assert level["db"]["main_loop_calls"] >= 8
        assert level["db"]["share"] > 0.0
        assert level["throughput_rps"] > 0.0


def test_cli_never_touches_a_real_redis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线的前提要能被证明：整条链路一次都不去拿真 Redis 客户端。"""

    from app.core import redis_client

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("压测入口不许连真 Redis")

    monkeypatch.setattr(redis_client, "get_redis_client", _explode)

    _code, payload, _raw = _run_cli(tmp_path)

    assert payload["levels"][0]["ok"] == 8


def test_cli_prints_a_markdown_report_that_can_be_committed(tmp_path: Path) -> None:
    """`--report-out` 写出来的就是 docs/ 下那份报告：命令、口径、三档表、占比、对照。"""

    from scripts import loadtest_baseline

    report_path = tmp_path / "report.md"
    code = loadtest_baseline.main(
        [
            "--levels",
            "2",
            "--requests",
            "6",
            "--warmup",
            "1",
            "--db-call-ms",
            "0.5",
            "--embed-ms",
            "1",
            "--vector-ms",
            "1",
            "--keyword-ms",
            "1",
            "--report-out",
            str(report_path),
        ]
    )

    assert code == 0
    report = report_path.read_text(encoding="utf-8")
    assert report.startswith("# ")
    assert "| 并发 |" in report
    assert "1177.9" in report
    assert "uv run python scripts/loadtest_baseline.py" in report
    assert math.isfinite(float(report.count("\n")))
