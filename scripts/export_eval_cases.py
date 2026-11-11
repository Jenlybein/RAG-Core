"""低分样本导出：从差评到候选评测题（#110）

用法::

    uv run python scripts/export_eval_cases.py --env-file ../RAG-Core/.env \\
        --max-score 3 --days 30 --out eval/datasets/candidates.json
    uv run python scripts/export_eval_cases.py ... --merge --kb-id kb-eval

**取数两半**：评分与归属来自本地两张表（`retrieval_feedback` + `retrieval_traces`），
问句原文**只能**从观测服务按 `trace_id` 取（`langfuse_reader` 的读接口）。
原文只在内存里过一遍，不写业务库（ADR-0021 决策 1）。

**观测不可用 = 明确失败**：没配凭据 / 连不上 / 这条 trace 查不到，一律非零退出并说清
缺哪一段，绝不产出缺 question 的条目或空候选集。这是本票最重要的行为。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ``--env-file`` 的预读收在 ``scripts/env_bootstrap.py``（三处同构实现已合并，见那边的说明）。
from scripts.env_bootstrap import preload_env_file as _preload_env_file  # noqa: E402


_ENV_FILE = _preload_env_file()

from app.core.config import settings  # noqa: E402
from scripts._export_eval_cases_core import (  # noqa: E402
    LowScoreSample,
    build_candidates,
    merge_candidates,
    select_low_scores,
    validate_candidates,
)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).isoformat()
    return str(value)


def load_samples() -> list[LowScoreSample]:
    """本地两表按 log_id 对齐，只读这些结构字段——**没有一列是问句原文**。"""

    from sqlalchemy import text

    from app.db.session import SessionLocal

    rows = SessionLocal().execute(
        text(
            "select f.log_id, f.score, f.created_at, t.tenant_id, t.trace_id, t.kb_ids "
            "from retrieval_feedback f join retrieval_traces t on t.id = f.log_id"
        )
    ).mappings().all()
    return [
        LowScoreSample(
            log_id=str(row["log_id"]),
            trace_id=str(row["trace_id"]),
            tenant_id=str(row["tenant_id"]),
            kb_ids=tuple(row["kb_ids"] or ()),
            score=int(row["score"]),
            created_at=_iso(row["created_at"]),
        )
        for row in rows
    ]


class _LangfuseQuerySource:
    """按 trace_id 取「这次检索的问句」；取不到就抛，让整轮明确失败。

    **读的是 observations，不是 trace**（这是本轮 code-review 修掉的第二处）：
    SDK v5 移除了 ``api.trace.get``（v4→v5 升级说明里明确列在 removed 一栏），
    而原文也不在 trace 级 input 里——v5 同时删掉了 trace IO setter，trace 的 input
    由根 observation 推出来，而我们六段是并列的兄弟 span、没有根。所以写入侧把
    ``query`` 放进**每一段** span 的 metadata（见 ``app.observability.langfuse_sink``），
    这里按 ``trace_id`` 取任意一段再读 ``metadata.query``。
    """

    def __init__(self) -> None:
        from app.observability.langfuse_reader import LangfuseReadError, _resolve_client

        try:
            self._client = _resolve_client()
        except LangfuseReadError as exc:
            raise RuntimeError(f"观测服务不可用（{exc.reason}）：拿不到问句原文，导出中止") from exc

    def query_of(self, trace_id: str) -> str:
        # 取数走 ``langfuse_reader.fetch_observations``，**不自己写一份**：``fields`` 漏了
        # 就永远读不到 metadata（这里表现为「没有问句原文」，瀑布图那边表现为「耗时全 0」），
        # 这个坑已经踩过一次，收在一处才不会踩第三次。
        from app.observability.langfuse_reader import LangfuseReadError, fetch_observations

        try:
            observations = fetch_observations(trace_id, client=self._client)
        except LangfuseReadError as exc:
            raise RuntimeError(
                f"trace {trace_id} 读取失败（{exc.reason}）：观测服务不可用，导出中止"
            ) from exc

        for observation in observations or ():
            metadata = getattr(observation, "metadata", None)
            if not isinstance(metadata, dict):
                continue
            query = metadata.get("query")
            if isinstance(query, str) and query.strip():
                return query
        raise RuntimeError(f"trace {trace_id}: 观测服务里没有问句原文，导出中止")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从低分反馈导出候选评测题")
    parser.add_argument("--env-file", default=_ENV_FILE)
    parser.add_argument("--max-score", type=int, default=3)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--kb-id", action="append", default=None, dest="kb_ids")
    parser.add_argument("--out", default="eval/datasets/candidates.json")
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--source", default="langfuse", choices=["langfuse"])
    args = parser.parse_args(argv)

    if not 1 <= args.max_score <= 5:
        print("--max-score 必须在 1..5 之间", file=sys.stderr)
        return 2

    try:
        samples = load_samples()
    except Exception as exc:  # noqa: BLE001 - 本地库都读不了，别往下走
        print(f"本地评分/归属读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    picked = select_low_scores(
        samples, max_score=args.max_score, days=args.days, now_epoch=time.time(), kb_filter=args.kb_ids
    )
    print(f"筛出低分记录 {len(picked)} 条（阈值 ≤{args.max_score}、最近 {args.days} 天）")

    # 观测服务不可用 / 某条 trace 没原文 → 这里抛，整轮非零退出，不落空文件。
    try:
        source = _LangfuseQuerySource()
        candidates = build_candidates(picked, query_of=source.query_of)
    except Exception as exc:  # noqa: BLE001
        print(f"导出中止：{exc}", file=sys.stderr)
        return 2

    existing: list[dict[str, Any]] = []
    out_path = Path(args.out)
    if args.merge and out_path.is_file():
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            existing = list(payload.get("questions", []))
        except (ValueError, KeyError) as exc:
            print(f"--merge 读不懂已有文件：{exc}", file=sys.stderr)
            return 2

    rows, added = merge_candidates(existing, candidates)
    problems = validate_candidates(rows)
    if problems:
        for problem in problems:
            print(f"产物自检失败：{problem}", file=sys.stderr)
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "name": "low_score_candidates",
                "k": settings.TOP_K,
                "questions": rows,
                "_note": "来自低分反馈的候选题；type=candidate、relevant 为空表示待人工标注",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    skipped = len(candidates) - len(added)
    print(f"写入 {out_path}：新增 {len(added)} 条、跳过重复 {skipped} 条、共 {len(rows)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
