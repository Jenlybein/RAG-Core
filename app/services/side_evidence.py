"""语义旁证：用 LLM 裁判跑固定 50 题子集，出 CP / CR（#109）。

**它是旁证，不是门禁**（ADR-0021 决策 5、GLOSSARY「旁证指标」）：裁判有随机性，同一份
结果两次打分可能不同，所以它**只提示、不阻断**。门禁是另一条路——ragas 的 ID 版双指标，
只吃两串 chunk id，零裁判、可复现（#107）。

三件事因此必须写在这里，而不是散在脚本里：

1. **裁判模型是度量衡，换一次就是换一把尺子。** 报告必须写明模型名，并在与上一份可比
   的报告里核对；换模型的结果标注「不可比」，绝不与旧数字并排放着让人误以为同尺。
2. **失败要留痕，不许悄悄变成 0 分。** 裁判超时 / 限流 / 返回不合法时，这道题记为
   ``error`` 并**从平均里排除**（不是按 0 分算）——把「裁判没评」算成「检索很烂」，
   会让一次账单问题看起来像一次检索退化。
3. **原文只在这里路过，不落库。** 裁判要读召回片段的正文，那是运行时内存里的东西；
   本模块不写任何正文到业务库（产物落 ``eval/runs/``，那是不进 git 的文件区）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.utils.report_format import as_metric

#: 旁证子集固定 50 题（#98 已定）。少于这个数不是「跑得快」，是「另一把尺子」。
SIDE_EVIDENCE_CASE_COUNT = 50


class Judge(Protocol):
    """裁判的最小形状：给定问题、召回片段、参考答案，返回 0~1 或 ``None``（没评上）。

    ``None`` 与 0.0 的差别是这张票最容易被做错的一处：前者是「裁判没给出结论」，
    后者是「裁判明确说完全不相关」。混同之后，一次限流就能让报告看起来像检索崩了。
    """

    @property
    def model(self) -> str:
        """裁判模型名（含大版本即可；报告里要能一眼看出换没换尺子）。"""

    async def context_precision(
        self, *, question: str, contexts: Sequence[str], reference: str
    ) -> float | None: ...

    async def context_recall(
        self, *, question: str, contexts: Sequence[str], reference: str
    ) -> float | None: ...


@dataclass(frozen=True)
class CaseResult:
    """一道旁证题的结果事实。``cp`` / ``cr`` 为 ``None`` 表示裁判没评上。"""

    id: str
    type: str
    question: str
    cp: float | None
    cr: float | None
    error: str | None = None

    @property
    def scored(self) -> bool:
        return self.cp is not None or self.cr is not None


#: 判分全部失败时的原因分类。顺序即优先级：**额度优先于凭据**——一轮里同时凑齐两件事时，
#: 「去查 Key」会把一个其实只是等额度的账号拖去翻配置。
_QUOTA_MARKERS = ("AccountQuotaExceeded", "429", "TooManyRequests", "quota")
_AUTH_MARKERS = ("401", "403", "Unauthorized", "invalid_api_key", "AuthenticationError")
#: 供应商在 429 里会写恢复时间（`reset at 2026-11-16 00:00:00 +0800 CST`）。
#: 抽取是**尽力而为**：抽不到就不提时间，绝不让它是这条诊断的前提。
_RESET_AT = re.compile(
    r"reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})"
)


def diagnose_all_unscored(errors: Sequence[str]) -> str:
    """一道题都没评上时，按**真实原因**给出一句话。

    为什么需要它（真实事故）：50 道题全部 429 `AccountQuotaExceeded`，而状态里写的是
    「检查裁判凭据 / 模型可用性」。于是人去翻 Key、翻 base_url、翻模型配置——全都不对，
    真正要做的是等额度恢复。同一类错误（把原因指错地方）在密钥那条路上也犯过。

    分类只做粗粒度，且**额度优先**：一轮里如果同时出现鉴权与额度问题，先说额度。
    """

    joined = " ".join(errors)
    if any(marker in joined for marker in _QUOTA_MARKERS):
        reset = _RESET_AT.search(joined)
        when = f"，额度将于 {reset.group(1)} 恢复" if reset else ""
        return (
            f"裁判配额已用尽（HTTP 429 AccountQuotaExceeded）{when}。"
            "这不是凭据或模型配置的问题：额度恢复前这道旁证取不到分数；"
            "门禁口径的 CP/CR（ID 匹配、不调 LLM）不受影响"
        )
    if any(marker in joined for marker in _AUTH_MARKERS):
        return (
            "裁判鉴权失败（401/403）：检查裁判模型的 API Key 与 base_url，"
            "以及模型配置里指向的那一项是否已停用"
        )
    return (
        "裁判一道题都没评上：逐题原因见 results.json 的 error 字段"
        "（常见原因：凭据 / 模型名 / 网络超时）"
    )


def aggregate(results: Sequence[CaseResult]) -> dict[str, Any]:
    """宏平均 + 分题型；**只对有效分数求平均**，未评分的题既不进分子也不进分母。

    这条口径与门禁那一侧同源（``#107`` 的「未评分不计入平均」），也是
    ``ACCEPTANCE.md`` 早就写过的教训：把 ``None`` 当 0 算，整体分会被一次裁判故障
    凭空拉低，读的人却以为是检索变差了。
    """

    def _mean(values: Sequence[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    def _of(items: Sequence[CaseResult]) -> dict[str, Any]:
        cps = [item.cp for item in items if item.cp is not None]
        crs = [item.cr for item in items if item.cr is not None]
        return {
            "count": len(items),
            "scored": sum(1 for item in items if item.scored),
            "unscored": sum(1 for item in items if not item.scored),
            "context_precision": _mean(cps),
            "context_recall": _mean(crs),
        }

    by_type = {
        type_name: _of([item for item in results if item.type == type_name])
        for type_name in sorted({item.type for item in results})
    }
    summary = _of(list(results))
    summary["by_type"] = by_type
    summary["errors"] = sorted(item.id for item in results if item.error)
    return summary


def build_report(
    *, run_id: str, judge_model: str, summary: Mapping[str, Any], results: Sequence[CaseResult]
) -> str:
    """人读报告。第一行就是「这是旁证、不阻断」与「哪把尺子」，防止被当成门禁读数。"""

    lines = [
        f"# 语义旁证报告 · {run_id}",
        "",
        "> **旁证指标：只提示、不阻断。** 门禁是确定性口径（ragas ID 版 CP/CR，零裁判、"
        "同配置两跑一致），那一份在 `eval/baseline/` 里。",
        ">",
        f"> 裁判模型：`{judge_model}`。**换裁判 = 换尺子**：与不同模型的报告不可直接比较，"
        "报告里会标「不可比」。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 | 已评分 | 未评分 |",
        "| --- | --- | --- | --- |",
        f"| context_precision | {_fmt(summary.get('context_precision'))} | "
        f"{summary.get('scored')} | {summary.get('unscored')} |",
        f"| context_recall | {_fmt(summary.get('context_recall'))} | "
        f"{summary.get('scored')} | {summary.get('unscored')} |",
        "",
        "## 分题型",
        "",
        "| 题型 | CP | CR | 已评分 |",
        "| --- | --- | --- | --- |",
    ]
    for type_name, block in (summary.get("by_type") or {}).items():
        lines.append(
            f"| {type_name} | {_fmt(block.get('context_precision'))} | "
            f"{_fmt(block.get('context_recall'))} | {block.get('scored')} |"
        )

    errors = [item for item in results if item.error]
    if errors:
        lines += ["", "## 裁判失败的题（**已从平均里排除**，不是 0 分）", "", "| 题 | 原因 |", "| --- | --- |"]
        lines += [f"| {item.id} | {item.error} |" for item in errors]

    lines += ["", "## 复现", "", "```powershell", "uv run python scripts/eval_side_evidence.py", "```", ""]
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    """报告里的分数渲染：``None`` 印成 ``n/a``，不是 0。"""

    return as_metric(value)


def write_status(
    *,
    out_dir: Path,
    run_id: str,
    status: str,
    error: str | None,
    judge_model: str,
    tenant_id: str | None,
    progress: Mapping[str, Any] | None = None,
) -> None:
    """写 ``status.json``——**原子替换**，因为它是别人正在读的唯一事实。

    跑批过程中每完成一题就更新一次进度，而控制台每 3 秒读一次同一个文件。直接
    ``write_text`` 会让读侧有机会读到写了一半的 JSON（`json.loads` 抛错 → 控制台把它当成
    「还没有结论」，于是进度闪回 ``queued``）。先写临时文件再 ``os.replace``：读侧要么看到
    旧的完整内容，要么看到新的完整内容。
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "error": error,
        "judge_model": judge_model,
        # 归属：控制台查状态时必须能证明「这次跑批是发起它那个租户的」。进度写入同样要带，
        # 否则中途那几秒里租户校验会因为缺字段而放行——那是一段真实的越权窗口。
        "tenant_id": tenant_id,
    }
    if progress is not None:
        payload["progress"] = dict(progress)

    target = out_dir / "status.json"
    temporary = out_dir / ".status.json.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)


def write_run(
    *, out_dir: Path, run_id: str, judge_model: str, summary: Mapping[str, Any],
    results: Sequence[CaseResult], status: str, error: str | None = None,
    tenant_id: str | None = None,
    progress: Mapping[str, Any] | None = None,
) -> None:
    """把一次旁证落成三个文件：报告、机读结果、状态。

    状态文件是控制台轮询的唯一依据：它由**跑批这一侧**写，而不是由触发它的请求写——
    于是「请求成功但跑批失败」不会被记成一次成功的旁证。``tenant_id`` 也写在这里，
    让读状态的人能证明归属（见 `write_status` 的注释）。
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(
        build_report(run_id=run_id, judge_model=judge_model, summary=summary, results=results),
        encoding="utf-8",
    )
    (out_dir / "results.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "judge_model": judge_model,
                "summary": dict(summary),
                "cases": [item.__dict__ for item in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_status(
        out_dir=out_dir,
        run_id=run_id,
        status=status,
        error=error,
        judge_model=judge_model,
        tenant_id=tenant_id,
        progress=progress,
    )


async def run_side_evidence(
    *,
    cases: Sequence[Mapping[str, Any]],
    judge: Judge,
    retrieve: Callable[[str], Awaitable[Sequence[str]]],
    out_dir: Path,
    run_id: str,
    limit: int | None = None,
    tenant_id: str | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    """跑一遍旁证并落盘；返回汇总。**异步 + 有界并发**。

    为什么是异步（一次实测换来的）：ragas 的 ContextPrecision / ContextRecall 不是
    每题一次调用，而是把每个片段拆成陈述句逐句判相关性——50 题粗算 1000+ 次请求，单次
    1.5 秒、全程 CPU 不到 1%。串行就是干等半小时。``concurrency`` 把「等」叠起来。

    ``retrieve`` 只回「这次真正召回到的片段正文」——**不要**拿标注切片当召回结果，
    那样两指标必然接近 1.0，等于自己跟自己比。裁判与检索都从参数进来，于是这层可以离线测。

    每完成一题就原子更新一次 ``status.json`` 的 ``progress``：控制台据此画进度条，而不是
    让「running」悬在那里让人猜还要多久。
    """

    selected = list(cases)[:limit] if limit else list(cases)
    total = len(selected)
    slots = max(1, concurrency)
    semaphore = asyncio.Semaphore(slots)
    done = 0

    def _progress() -> None:
        write_status(
            out_dir=out_dir,
            run_id=run_id,
            status="running",
            error=None,
            judge_model=judge.model,
            tenant_id=tenant_id,
            progress={"done": done, "total": total},
        )

    # 先写一次 0/total：界面在第一批结果回来之前就该知道「一共多少题」。
    _progress()

    async def _one(index: int, case: Mapping[str, Any]) -> CaseResult:
        nonlocal done
        question = str(case.get("question") or "")
        reference = str(case.get("answer") or "")
        try:
            async with semaphore:
                contexts = list(await retrieve(question))
                cp = await judge.context_precision(
                    question=question, contexts=contexts, reference=reference
                )
                cr = await judge.context_recall(
                    question=question, contexts=contexts, reference=reference
                )
            return CaseResult(
                id=str(case.get("id") or ""),
                type=str(case.get("type") or "unknown"),
                question=question,
                cp=cp,
                cr=cr,
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败不该炸掉整轮，但必须留痕
            return CaseResult(
                id=str(case.get("id") or ""),
                type=str(case.get("type") or "unknown"),
                question=question,
                cp=None,
                cr=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            done += 1
            _progress()

    # `gather` 保序：结果与 `selected` 一一对应，报告的顺序因此不随调度漂移。
    results = list(await asyncio.gather(*(_one(i, c) for i, c in enumerate(selected))))

    summary = aggregate(results)
    scored = int(summary.get("scored") or 0)
    unscored = int(summary.get("unscored") or 0)
    # 三档状态，不是「绿 / 红」两档。中间那档是这轮最真实的情况：5 小时配额打到一半，
    # 前几题评上了、后面全 429。写成 ok 会让读报告的人以为 50 题都算过；写成 failed
    # 又抹掉了那几题有效分。partial 把两件事都说清楚，退出码也单独一档。
    if scored == 0:
        status = "failed"
        # 按真实原因说话：50 题全 429 时写「检查裁判凭据」会把人送去翻一个没问题的配置。
        error = diagnose_all_unscored([r.error for r in results if r.error])
    elif unscored > 0:
        status = "partial"
        error = f"{unscored} 道题没评上（裁判配额 / 超时 / 限流），已从平均里排除"
    else:
        status = "ok"
        error = None
    write_run(
        out_dir=out_dir, run_id=run_id, judge_model=judge.model, summary=summary,
        results=results, status=status, error=error, tenant_id=tenant_id,
        progress={"done": total, "total": total},
    )
    return {"summary": summary, "status": status, "error": error}


__all__ = [
    "CaseResult",
    "Judge",
    "SIDE_EVIDENCE_CASE_COUNT",
    "aggregate",
    "build_report",
    "run_side_evidence",
    "write_run",
]
