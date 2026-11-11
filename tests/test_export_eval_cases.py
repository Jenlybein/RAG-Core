"""低分样本导出的离线测试（#110）。

全是纯函数或可注入依赖，不连本地库、不连观测服务、不发网络。重点钉三件事：
原文取不到就整轮失败、原文不写进业务库（产物字段里只有问句本身）、增量合并去重。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from scripts._export_eval_cases_core import (
    CANDIDATE_TYPE,
    Candidate,
    LowScoreSample,
    as_case,
    build_candidates,
    merge_candidates,
    normalize_query,
    select_low_scores,
    validate_candidates,
)


def _sample(log_id: str, trace_id: str, score: int = 2, *, days_ago: float = 1) -> LowScoreSample:
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return LowScoreSample(
        log_id=log_id,
        trace_id=trace_id,
        tenant_id="tenant_demo",
        kb_ids=("kb-eval",),
        score=score,
        created_at=stamp.isoformat(),
    )


def test_normalize_query_collapses_whitespace_only() -> None:
    # 中文问法里的空格都是噪声：三种写法是同一句差评。
    assert normalize_query("  退款　多久  到账 \n") == "退款多久到账"
    assert normalize_query("退款多久到账") == normalize_query("退款 多久到账")
    # 大小写 / 型号不被改动：它们是区分信息。
    assert normalize_query("E1024 怎么处理") == "E1024怎么处理"
    assert normalize_query("E1024 怎么处理") != normalize_query("e1024怎么处理")


def test_select_low_scores_filters_by_score_time_and_kb() -> None:
    samples = [
        _sample("a", "t-a", score=2, days_ago=1),
        _sample("b", "t-b", score=4, days_ago=1),          # 分太高
        _sample("c", "t-c", score=1, days_ago=45),          # 太旧
        _sample("d", "t-d", score=3, days_ago=2),
    ]

    picked = select_low_scores(samples, max_score=3, days=30, now_epoch=time.time())

    assert {item.log_id for item in picked} == {"a", "d"}

    by_kb = select_low_scores(
        samples, max_score=3, days=30, now_epoch=time.time(), kb_filter=["kb-other"]
    )
    assert by_kb == []


def test_build_candidates_fails_loudly_when_any_query_is_missing() -> None:
    """观测服务取不到原文 = 整轮失败，绝不产出一条缺 question 的候选。"""

    def query_of(trace_id: str) -> str:
        if trace_id == "t-b":
            raise RuntimeError("观测服务不可用")
        return f"问句 {trace_id}"

    with pytest.raises(RuntimeError, match="观测服务不可用"):
        build_candidates([_sample("a", "t-a"), _sample("b", "t-b")], query_of=query_of)


def test_build_candidates_rejects_an_empty_query() -> None:
    with pytest.raises(RuntimeError, match="空的问句原文"):
        build_candidates([_sample("a", "t-a")], query_of=lambda _: "   ")


def test_candidate_shape_is_a_question_with_empty_relevant() -> None:
    """`relevant: []` 是待人工标注，不是「库里没有答案」；校验器要认这个形状。"""

    candidate = Candidate(
        id="fb-a", query="退款多久到账", trace_id="t-a", log_id="a", kb_ids=("kb-eval",), score=2
    )
    row = as_case(candidate)

    assert row["type"] == CANDIDATE_TYPE
    assert row["relevant"] == []
    assert row["query"] == "退款多久到账"
    assert row["source"]["trace_id"] == "t-a"
    # 原文只以 query 一个字段出现，不复制进 source / note / 其它字段。
    assert "退款多久到账" not in json.dumps(row["source"], ensure_ascii=False)
    assert validate_candidates([row]) == []


def test_validate_candidates_rejects_a_nonempty_relevant() -> None:
    row = as_case(
        Candidate(id="fb-a", query="q", trace_id="t", log_id="a", kb_ids=(), score=1)
    )
    row["relevant"] = [{"document": "x", "heading_path": "y"}]

    assert any("relevant 必须是空列表" in problem for problem in validate_candidates([row]))


def test_merge_deduplicates_by_normalized_query_not_by_id() -> None:
    """同一句问句被两次差评，只该进题库一次（id 不同也要跳过）。"""

    existing: list[dict[str, Any]] = [
        as_case(Candidate(id="fb-a", query="退款多久到账", trace_id="t-a", log_id="a", kb_ids=(), score=1))
    ]
    new = [
        Candidate(id="fb-b", query="退款 多久到账", trace_id="t-b", log_id="b", kb_ids=(), score=2),
        Candidate(id="fb-c", query="发票几天开", trace_id="t-c", log_id="c", kb_ids=(), score=3),
    ]

    rows, added = merge_candidates(existing, new)

    assert [item.id for item in added] == ["fb-c"]
    assert len(rows) == 2
    assert validate_candidates(rows) == []


def test_cli_writes_a_file_that_shape_check_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 用假的取数与原文源跑一遍，产物落盘后能过形状自检。"""

    import scripts.export_eval_cases as cli

    monkeypatch.setattr(cli, "load_samples", lambda: [_sample("a", "t-a")])

    class _FakeSource:
        def query_of(self, trace_id: str) -> str:
            return "退款多久到账"

    monkeypatch.setattr(cli, "_LangfuseQuerySource", lambda: _FakeSource())
    out = tmp_path / "candidates.json"

    code = cli.main(["--out", str(out), "--days", "30", "--max-score", "3"])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["questions"]) == 1
    assert validate_candidates(payload["questions"]) == []


def test_cli_does_not_write_anything_when_the_observation_service_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """票面最重要的一条：观测不可用 = 明确失败，且不产出空候选集。"""

    import scripts.export_eval_cases as cli

    monkeypatch.setattr(cli, "load_samples", lambda: [_sample("a", "t-a")])

    def _boom() -> Any:
        raise RuntimeError("观测服务不可用（not_configured）：拿不到问句原文，导出中止")

    monkeypatch.setattr(cli, "_LangfuseQuerySource", _boom)
    out = tmp_path / "candidates.json"

    code = cli.main(["--out", str(out), "--days", "30"])

    assert code == 2
    assert not out.exists(), "观测挂了就不该落一个空文件"
