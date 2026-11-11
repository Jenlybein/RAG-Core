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
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _preload_env_file() -> str | None:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=None)
    known, _ = parser.parse_known_args()
    if not known.env_file:
        return None
    from dotenv import dotenv_values

    for key, value in dotenv_values(known.env_file).items():
        if value is not None:
            os.environ.setdefault(key, value)
    return known.env_file


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

    观测服务的 trace 上，问句写在 trace 的 input 里（`langfuse_sink` 推 span 时带的是
    属性，不含原文；原文在 trace 级 input）。这里用 REST 客户端读 trace，再取 input.query。
    """

    def __init__(self) -> None:
        from app.core.config import settings as cfg
        from app.observability.langfuse_reader import LangfuseReadError, _resolve_client

        try:
            self._client = _resolve_client()
        except LangfuseReadError as exc:
            raise RuntimeError(f"观测服务不可用（{exc.reason}）：拿不到问句原文，导出中止") from exc

    def query_of(self, trace_id: str) -> str:
        from app.observability.langfuse_reader import LangfuseReadError

        try:
            trace = self._client.trace.get(trace_id)
        except LangfuseReadError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"trace {trace_id} 读取失败：观测服务不可用，导出中止") from exc
        payload = getattr(trace, "input", None)
        if isinstance(payload, dict):
            query = payload.get("query")
            if isinstance(query, str) and query.strip():
                return query
        if isinstance(payload, str) and payload.strip():
            return payload
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
