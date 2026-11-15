"""C6 守分抽样：既有 137 题基线上**抽 20 题**跑一次，逐题与历史基线对账（#153 §③-4）。

## 为什么不是直接跑 ``eval_runner run --limit 20``

那条路现在跑不通（本票在真库上撞到）：``scripts/eval_runner.py`` 的 ``_run`` 仍用
**同步** Session 打开数据库，而 ``rag_service.retrieve`` 从 #130 / #132 起是异步链路——
第一条查询上就是 ``TypeError: object ChunkedIteratorResult can't be used in 'await' expression``。
仓储层迁移是那次改造的一部分，评测运行器没跟着迁。这件事比数字更重要，所以它进了报告首页。

把整套运行器改成异步是另一张票的规模（要动清空 / 建库 / 建文档 / 索引 / 标签解析六处），
本票按「大修停下报告」的纪律不碰它。这里改为**只补一条窄路**：

```text
读 20 道题（与 --limit 20 同一批：included[:20]）
   → 异步读已索引切片（同一批行、同一组库）
   → 复用 eval_retrieval._resolve_labels（纯函数，不碰库）
   → 走生产检索链路（scripts.eval_refs.run_live：async + 同一个 retrieve）
   → 用 eval_runner.context_scores 打分（**同一把尺子**：ragas ID 版 CP / CR）
   → 与 eval/baseline/results.json 逐题对账
```

**尺子没换**：分数由与历史基线同一个函数（``eval_runner.context_scores``）算出，标签由
同一份 ``_resolve_labels`` 解析。换掉的只有「会话是同步还是异步」这一层管道——那不是尺子。

## 它下什么结论、不下什么结论

* **只**回答「这 20 题有没有掉分 / 报错 / 破上限」，**不**外推到 137 题：
  抽样是抽样，``sample`` 字段把 n=20 写进产物，报告里也不许写「整体不退步」。
* 逐题比：``context_precision`` / ``context_recall`` 各自与基线那一题的值比。判据沿用
  ``eval_runner`` 的门禁口径（整体 ≥ −1.00pp、单题型退化 ≤ 3.00pp），但**只作为参考线**：
  20 题的样本量撑不起门禁结论，所以它输出的结论词是「抽样无退化 / 抽样有退化」。
* 检索报错（``error``）与「空结果」分开记：前者是坏，后者是读数。

用法::

    uv run python scripts/eval_c6_guard.py                 # 跑 20 题（要库、要嵌入凭据，约 20 次嵌入）
    uv run python scripts/eval_c6_guard.py --limit 5       # 通路检查
    uv run python scripts/eval_c6_guard.py --dry-run       # 只打印选中的题，不连库

退出码：0 = 抽样无退化；1 = 有退化或检索报错；2 = 前置条件不满足（题库 / 基线读不到）。
模块顶层不解析 argv、不连数据库，导入即安全；比较逻辑是纯函数，``tests/test_eval_c6_guard.py`` 直接测。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASELINE_DIR = REPO_ROOT / "eval" / "baseline"
BASELINE_RESULTS = BASELINE_DIR / "results.json"
OUTPUT_DIR = REPO_ROOT / "eval" / "reports" / "c6"

#: 与 ``eval_runner`` 的门禁阈值同口径（只作参考线，见模块文档）。
OVERALL_PP = -1.00
PER_TYPE_PP = -3.00

#: 退化判据的容差：两个浮点数在不同进程里算同一条公式也可能差最后一位，
#: 拿 ``==`` 判「没变」会把一次无关的浮点抖动报成退化。
EPSILON = 1e-9


@dataclass(frozen=True)
class GuardRow:
    """一道题的对照行：基线分、抽样分、差值与判定。"""

    id: str
    type: str
    baseline_precision: float | None
    baseline_recall: float | None
    precision: float | None
    recall: float | None
    returned: int
    reference: int
    error: str | None = None
    unscored_reason: str | None = None
    degraded_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "baseline": {
                "context_precision": self.baseline_precision,
                "context_recall": self.baseline_recall,
            },
            "sample": {"context_precision": self.precision, "context_recall": self.recall},
            "delta": {
                "context_precision": _delta(self.baseline_precision, self.precision),
                "context_recall": _delta(self.baseline_recall, self.recall),
            },
            "returned": self.returned,
            "reference": self.reference,
            "error": self.error,
            "unscored_reason": self.unscored_reason,
            "degraded_reason": self.degraded_reason,
        }


def _delta(before: float | None, after: float | None) -> float | None:
    """``after − before``；任一侧缺失就是 ``None``（缺失不是 0）。"""

    if before is None or after is None:
        return None
    return after - before


def _mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


@dataclass(frozen=True)
class GuardVerdict:
    """抽样的总判定：掉了几题、哪个题型掉得最多、结论词。"""

    rows: list[GuardRow] = field(default_factory=list)

    @property
    def scored(self) -> list[GuardRow]:
        return [
            row
            for row in self.rows
            if row.precision is not None and row.baseline_precision is not None
        ]

    @property
    def degraded(self) -> list[GuardRow]:
        """检索降级（成功但少了一路）的题：分数变化要能对上「哪一路没跑」。"""

        return [row for row in self.rows if row.degraded_reason]

    @property
    def failures(self) -> list[GuardRow]:
        """检索直接报错的题：它不是「掉分」，是「没有读数」。"""

        return [row for row in self.rows if row.error]

    def overall(self) -> dict[str, dict[str, float | None]]:
        scored = self.scored
        return {
            "context_precision": {
                "baseline": _mean([row.baseline_precision for row in scored]),
                "sample": _mean([row.precision for row in scored]),
                "delta_pp": _pp(
                    _delta(
                        _mean([row.baseline_precision for row in scored]),
                        _mean([row.precision for row in scored]),
                    )
                ),
            },
            "context_recall": {
                "baseline": _mean([row.baseline_recall for row in scored]),
                "sample": _mean([row.recall for row in scored]),
                "delta_pp": _pp(
                    _delta(
                        _mean([row.baseline_recall for row in scored]),
                        _mean([row.recall for row in scored]),
                    )
                ),
            },
        }

    def by_type(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[GuardRow]] = {}
        for row in self.scored:
            grouped.setdefault(row.type, []).append(row)
        return {
            name: {
                "questions": len(rows),
                "context_precision": _mean([row.precision for row in rows]),
                "context_recall": _mean([row.recall for row in rows]),
                "delta_precision_pp": _pp(
                    _delta(
                        _mean([row.baseline_precision for row in rows]),
                        _mean([row.precision for row in rows]),
                    )
                ),
                "delta_recall_pp": _pp(
                    _delta(
                        _mean([row.baseline_recall for row in rows]),
                        _mean([row.recall for row in rows]),
                    )
                ),
            }
            for name, rows in sorted(grouped.items())
        }

    def regressions(self) -> list[GuardRow]:
        """任何一侧掉了（超过容差）的题——「不变差」的唯一判据。"""

        worsened: list[GuardRow] = []
        for row in self.scored:
            for before, after in (
                (row.baseline_precision, row.precision),
                (row.baseline_recall, row.recall),
            ):
                delta = _delta(before, after)
                if delta is not None and delta < -EPSILON:
                    worsened.append(row)
                    break
        return worsened

    def verdict(self) -> str:
        if self.failures:
            return "抽样有报错（不是掉分，是没有读数）"
        if self.regressions():
            return "抽样有退化"
        return "抽样无退化"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_size": len(self.rows),
            "scored_size": len(self.scored),
            "verdict": self.verdict(),
            "overall": self.overall(),
            "by_type": self.by_type(),
            "regressions": [row.id for row in self.regressions()],
            "failures": [{"id": row.id, "error": row.error} for row in self.failures],
            "degraded": [{"id": row.id, "reason": row.degraded_reason} for row in self.degraded],
            "rows": [row.as_dict() for row in self.rows],
            "thresholds_pp": {"overall": OVERALL_PP, "per_type": PER_TYPE_PP},
            "note": (
                "抽样（n=20），不是 137 题整体：结论只能读作「这 20 题没有退化」，"
                "不外推整体、也不进基线门禁。"
            ),
        }


def _pp(delta: float | None) -> float | None:
    return None if delta is None else delta * 100.0


def compare(
    *,
    baseline_questions: Sequence[Mapping[str, Any]],
    sample_rows: Sequence[Mapping[str, Any]],
) -> GuardVerdict:
    """基线逐题明细 + 本次抽样读数 → 对照表（纯函数：两侧都是普通映射）。"""

    baseline = {str(item["id"]): item for item in baseline_questions}
    rows: list[GuardRow] = []
    for item in sample_rows:
        question_id = str(item["id"])
        base = baseline.get(question_id, {})
        rows.append(
            GuardRow(
                id=question_id,
                type=str(item.get("type") or base.get("type") or ""),
                baseline_precision=base.get("context_precision"),
                baseline_recall=base.get("context_recall"),
                precision=item.get("context_precision"),
                recall=item.get("context_recall"),
                returned=int(item.get("returned_count") or 0),
                reference=int(item.get("reference_count") or 0),
                error=item.get("error"),
                unscored_reason=item.get("unscored_reason"),
                degraded_reason=item.get("degraded_reason"),
            )
        )
    return GuardVerdict(rows=rows)


# ─────────────────────────────────────────────────────────────── 真实跑批


async def _load_indexed_chunks() -> list[Any]:
    """异步版的「读已索引切片」（``eval_retrieval._load_indexed_chunks`` 的同一批行）。

    只换会话类型：选的是同一组库、同一个租户，字段也一一对应（``IndexedChunk`` 那个
    数据类原样复用，标签解析因此仍是同一份实现）。
    """

    from sqlalchemy import select

    from app.core.config import settings
    from app.db.async_session import AsyncSessionLocal
    from app.models.chunk import Chunk
    from scripts.eval_retrieval import IndexedChunk, _eval_kb_ids

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Chunk).where(
                        Chunk.tenant_id == settings.DEV_TENANT_ID,
                        Chunk.kb_id.in_(list(_eval_kb_ids())),
                    )
                )
            )
            .scalars()
            .all()
        )

    chunks: list[IndexedChunk] = []
    for row in rows:
        meta = row.meta or {}
        chunks.append(
            IndexedChunk(
                chunk_id=row.id,
                document_id=row.document_id,
                kb_id=row.kb_id,
                heading_path=str(meta.get("heading_path") or ""),
                order_index=int(meta.get("chunk_index") or 0),
                content_head=str(row.content or "")[:120],
            )
        )
    return chunks


async def _run_sample(*, limit: int) -> list[dict[str, Any]]:
    """抽前 ``limit`` 道进平均的题，走生产检索链路并打分。"""

    from scripts import eval_refs, eval_retrieval, eval_runner
    from app.core.config import settings

    questions, _k = eval_retrieval._load_questions()
    included = [q for q in questions if q.type not in eval_runner.EXCLUDED_QUESTION_TYPES]
    selected = included[:limit]

    chunks = await _load_indexed_chunks()
    labels, _traps = eval_retrieval._resolve_labels(questions, chunks)
    kb_ids = [layout.kb_id for layout in eval_retrieval._load_manifest()[0]]

    outcomes = await eval_refs.run_live(
        questions=[{"id": q.id, "query": q.query, "mechanism": q.type} for q in selected],
        kb_ids=kb_ids,
        top_k=5,
        tenant_id=settings.DEV_TENANT_ID,
        graph="off",
        research="off",
        # 与历史基线同一口径：重排关着（eval/baseline/environment.json
        # 的 rerank_enabled=false）。抽样要比的是「有没有变差」，不是「换了把尺子」。
        rerank=eval_refs.RERANK_OFF,
    )

    rows: list[dict[str, Any]] = []
    for question, outcome in zip(selected, outcomes):
        reference = [str(chunk_id) for chunk_id in labels.get(question.id, [])]
        returned = [hit.chunk_id for hit in outcome.hits]
        precision: float | None = None
        recall: float | None = None
        reason: str | None = None
        error = outcome.error
        if error is None and not reference:
            reason = "no_reference_ids"
        elif error is None:
            precision, recall = await eval_runner.context_scores(returned, reference)
            if precision is None:
                reason = "no_retrieved_context"
        rows.append(
            {
                "id": question.id,
                "type": question.type,
                "query": question.query,
                "returned_count": len(returned),
                "reference_count": len(reference),
                "context_precision": precision,
                "context_recall": recall,
                "unscored_reason": reason,
                "error": error,
                "empty_reason": outcome.empty_reason,
                "degraded_reason": outcome.degraded_reason,
                "duration_ms": outcome.latency_ms,
                "model_calls": outcome.model_calls,
                "returned_ids": returned,
                "reference_ids": reference,
            }
        )
    return rows


def render_markdown(payload: Mapping[str, Any]) -> str:
    """人读的抽样报告：判定、逐题型、逐题三张表，外加「这是抽样」的显式声明。"""

    overall = payload["overall"]
    lines = [
        f"# C6 守分抽样（{payload['run_id']}）",
        "",
        f"- 抽样：**{payload['sample_size']} 题**（137 题基线里前 {payload['sample_size']} 道"
        "进平均的题，与 `eval_runner run --limit` 同一批）",
        f"- 结论：**{payload['verdict']}**",
        f"- 基线：`eval/baseline/results.json`（run `{payload.get('baseline_run_id', '?')}`）",
        "- 口径：ragas ID 版 `context_precision` / `context_recall`（零 LLM），"
        "与历史基线**同一个函数**算出来",
        (
            f"- 检索降级：**{len(payload['degraded'])} 题**（`hybrid` 少了某一路——"
            "见逐题的「降级」列；分数变化必须能对上这一栏）"
            if payload.get("degraded")
            else "- 检索降级：无（两路都跑完了）"
        ),
        "",
        "> **这是抽样，不是 137 题整体。** 结论只能读作「这 20 题没有退化」，不外推整体、"
        "也不进基线门禁。原来的 137 题跑批路径本轮不可用（见报告首页），所以只抽了 20 题。",
        "",
        "## 整体",
        "",
        "| 指标 | 基线（同样这 20 题） | 本次 | Δ(pp) | 参考阈值 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, row in overall.items():
        lines.append(
            f"| {name} | {_fmt(row['baseline'])} | {_fmt(row['sample'])} "
            f"| {_fmt_pp(row['delta_pp'])} | ≥ {payload['thresholds_pp']['overall']:.2f}pp |"
        )
    lines += [
        "",
        "## 分题型",
        "",
        "| 题型 | 题数 | CP | CR | ΔCP(pp) | ΔCR(pp) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, row in payload["by_type"].items():
        lines.append(
            f"| {name} | {row['questions']} | {_fmt(row['context_precision'])} "
            f"| {_fmt(row['context_recall'])} | {_fmt_pp(row['delta_precision_pp'])} "
            f"| {_fmt_pp(row['delta_recall_pp'])} |"
        )
    lines += [
        "",
        "## 逐题",
        "",
        "| 题号 | 题型 | 基线 CP/CR | 本次 CP/CR | 返回 | 报错 / 未评分 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["rows"]:
        base = f"{_fmt(row['baseline']['context_precision'])}/{_fmt(row['baseline']['context_recall'])}"
        now = f"{_fmt(row['sample']['context_precision'])}/{_fmt(row['sample']['context_recall'])}"
        note = (
            row.get("error")
            or row.get("unscored_reason")
            or (f"降级：{row['degraded_reason']}" if row.get("degraded_reason") else "—")
        )
        lines.append(
            f"| {row['id']} | {row['type']} | {base} | {now} | {row['returned']} | {note} |"
        )
    regressions = payload["regressions"]
    lines += [
        "",
        "## 退化项",
        "",
        (
            "没有退化项：这 20 题的 CP / CR 与历史基线逐题相同。"
            if not regressions
            else "退化题号：" + "、".join(f"`{item}`" for item in regressions)
        ),
        "",
    ]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _fmt_pp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def write_outputs(payload: Mapping[str, Any], *, out_dir: Path = OUTPUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"c6-guard-{payload['run_id']}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / f"{stem}.md").write_text(render_markdown(payload), encoding="utf-8")
    return out_dir / stem


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "C6 guard sample: run the first N gate questions through the production "
            "retrieval chain and compare per-question with eval/baseline."
        )
    )
    parser.add_argument("--limit", type=int, default=20, help="抽多少题（默认 20）")
    parser.add_argument("--baseline", default=str(BASELINE_RESULTS), help="基线 results.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印选中的题，不连库")
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"读不到基线：{baseline_path}", file=sys.stderr)
        return 2

    from scripts import eval_retrieval, eval_runner

    questions, _k = eval_retrieval._load_questions()
    included = [q for q in questions if q.type not in eval_runner.EXCLUDED_QUESTION_TYPES]
    selected = included[: args.limit]
    if args.dry_run:
        print(f"抽 {len(selected)} 道题（不连库）：")
        for question in selected:
            print(f"  {question.id} {question.type} {question.query[:40]}")
        return 0

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    print(f"预计模型调用：{len(selected)} 次查询嵌入（重排按基线口径关着）")
    rows = asyncio.run(_run_sample(limit=args.limit))
    verdict = compare(baseline_questions=baseline.get("per_question", []), sample_rows=rows)
    payload = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_run_id": baseline.get("run_id"),
        **verdict.as_dict(),
        "model_calls": sum(int(row.get("model_calls") or 0) for row in rows),
        "note_runner": (
            "既有 eval_runner 的 live 路径本轮不可用（同步 Session 撞异步检索链路），"
            "所以抽样走 scripts/eval_c6_guard.py 这条窄路：会话换成异步，检索与打分仍是"
            "同一条生产链路与同一个打分函数。"
        ),
    }
    target = write_outputs(payload)
    print(f"结论：{payload['verdict']}（{payload['scored_size']}/{payload['sample_size']} 题评分）")
    print(f"产物：{target.with_suffix('.md').relative_to(REPO_ROOT)}")
    return 0 if payload["verdict"] == "抽样无退化" else 1


if __name__ == "__main__":
    raise SystemExit(main())
