"""语义旁证跑批的命令行入口（#109）。

接线都在 :mod:`app.services.side_evidence_run`，这里只做三件事：读题、调一次、打印。
控制台触发的也是同一个 ``run_once``，所以「命令行跑出来的」与「面板跑出来的」是同一份产物。

用法::

    uv run python scripts/eval_side_evidence.py --env-file ../RAG-Core/.env   # 全量
    uv run python scripts/eval_side_evidence.py --env-file ../RAG-Core/.env --limit 3
    uv run python scripts/eval_side_evidence.py --out eval/runs/side-evidence/manual

**为什么必须支持 ``--env-file``**：``settings`` 是模块级单例，导入即定型；而 worktree 里
通常没有 ``.env``（离线套件的前提）。没有它，嵌入与关键词两路都拿不到配置，检索会以
「两路都失败」收场——看起来像检索坏了，其实是没配。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ``--env-file`` 的预读收在 ``scripts/env_bootstrap.py``（三处同构实现已合并，见那边的说明）。
from scripts.env_bootstrap import preload_env_file as _preload_env_file  # noqa: E402


_ENV_FILE = _preload_env_file()

from app.core.config import settings  # noqa: E402
from app.services.side_evidence import SIDE_EVIDENCE_CASE_COUNT  # noqa: E402
from app.services.side_evidence_run import new_run_id, run_once  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语义旁证：固定 50 题、LLM 裁判（不进闸门）")
    parser.add_argument("--env-file", default=_ENV_FILE)
    parser.add_argument("--answers", default="eval/answers_v1.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    # 与 eval_retrieval 一致：把这次评测要用的旋钮钉死，不吃机器上的偶然值
    # （尤其是 ES 索引——不钉就会去查生产索引，旁证测的就不是评测语料了）。
    from scripts.eval_retrieval import _isolate_settings

    _isolate_settings(top_k=settings.TOP_K)

    answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    cases = [case for case in answers.get("cases", []) if case.get("status") == "ok"]
    if len(cases) < SIDE_EVIDENCE_CASE_COUNT:
        print(f"⚠ 可用答案 {len(cases)} 条，少于定盘值 {SIDE_EVIDENCE_CASE_COUNT} 条")

    run_id = new_run_id()
    out_dir = Path(args.out or f"eval/runs/side-evidence/{run_id}")
    result = run_once(cases=cases, out_dir=out_dir, run_id=run_id, limit=args.limit)

    block = result["summary"]
    print(f"状态：{result['status']}  报告：{out_dir / 'report.md'}")
    print(
        f"context_precision={block.get('context_precision')} "
        f"context_recall={block.get('context_recall')} "
        f"已评分={block.get('scored')} 未评分={block.get('unscored')}"
    )
    if result.get("error"):
        print(f"原因：{result['error']}")
    # partial 也是非零：控制台要把「只跑了一部分」当成一次需要人看一眼的结果，而不是绿。
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
