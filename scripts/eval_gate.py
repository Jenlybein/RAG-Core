"""门禁的纯函数：算分口径、未评分排除、相对判据、报告渲染、数据集与配置校验。

**为什么单独成一个模块**：门禁是「阻断发布」的那一侧。它算错一次，要么把退化放进主干
（假绿），要么把没退化的一次拦住（假红）；两种错都不会让检索链路报错，只会静默地
误导人。所以这里只放**纯函数**——不连数据库、不连 Elasticsearch、不发网络、不读
``settings``，于是可以用手算真值表把每条口径钉死（见 ``tests/test_eval_gate.py``）。

三条口径写在这里，别处不许再算一遍：

1. **门禁指标**是 ``context_precision`` / ``context_recall``，实现用 ragas 的 **ID 版**
   （只需 ``retrieved_context_ids`` + ``reference_context_ids``，**不需要 LLM、不需要
   标准答案文本**）。:func:`context_scores` 是唯一入口。
2. **未评分不计入平均**：``None`` / ``NaN`` 的记录单列并跳过，按**指标**分别跳过——
   一条记录 CP 没评上分时，它的 CR 仍然可以是真实的 0 分（返回空就是没召回），
   把 CR 也一起丢掉会让「检索全空」的一次跑出满绿。
3. **判据是相对基线**，不是绝对分数线：整体不低于基线 −1pp、单题型退化不超过 3pp。
   绝对分数线会在换语料 / 换库时整片误报（见 #98）。

算分本身不在这里重写：``P@k`` / ``R@k`` / ``MRR`` 仍由 ``scripts/eval_metrics.py``
的纯函数算；本模块只补「门禁要看的那几个数」。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 门禁只看这两个指标。名字固定：报告、教程、口头沟通用同一个词（GLOSSARY「门禁指标」）。
GATE_METRICS: tuple[str, str] = ("context_precision", "context_recall")

#: 不进主平均的题型。
#:
#: * ``absent``（6 道）：库里确实没有，正确答案是「一条都不返回」——它测的是**误召**，
#:   不是召回（``scripts/eval_metrics.aggregate`` 按「相关为空」排除，同一条规则）。
#: * ``false_premise``（4 道）：问题本身的前提是错的，正确行为是识别前提有误；
#:   检索链路只会返回「最像问题本身」的切片，拿它算 CP/CR 等于用一个量不出这件事的尺子
#:   去量（``scripts/eval_chunking.EXCLUDED_FROM_MAIN`` 同一条规则）。
EXCLUDED_QUESTION_TYPES: frozenset[str] = frozenset({"absent", "false_premise"})

#: 整体退化上限：不低于基线 −1pp。
DEFAULT_OVERALL_PP: float = 1.0
#: 单题型退化上限：不超过 3pp。
DEFAULT_PER_TYPE_PP: float = 3.0

#: 比较「百分点差」时的浮点容差。恰好 −1.00pp 判通过：0.80 − 0.81 在二进制里是
#: −0.010000000000000009，不留容差会把「恰好落在边界」判成红。
PP_TOLERANCE: float = 1e-6

#: 报告与 ``--select`` 的命令前缀（报告里每个数字都带一条完整的这种命令）。
RUNNER_COMMAND = "uv run python scripts/eval_runner.py"

_RUN_SCHEMA = "eval-run/1"


# ─────────────────────────────────────────────────────────────── 单题的一条记录


@dataclass(frozen=True)
class QuestionScore:
    """一道题在两个门禁指标上的结果；``None`` 表示**未评分**（不是 0 分）。"""

    id: str
    type: str
    context_precision: float | None = None
    context_recall: float | None = None
    #: 未评分的原因（``retrieval_failed`` / ``no_retrieved_context`` …），会进报告。
    unscored_reason: str | None = None


def is_scored(value: float | None) -> bool:
    """``0.0`` 是已评分，``None`` 与 ``NaN`` 不是。

    这条区分是整个「未评分不计入平均」的地基：0 分会被算进平均，NaN 不会。
    """

    if value is None:
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):  # pragma: no cover - 只有上游塞了非数才走到
        return False


def mean_scored(values: Iterable[float | None]) -> float | None:
    """只对已评分的值求平均；一个都没有时回 ``None``（不是 0）。

    ``None`` 与 0 的区别是「没考」与「考了全错」：回 0 会让一次什么都没测到的运行
    看起来像考砸了，回 None 才会让调用方停下来问「为什么没测到」。
    """

    scored = [float(value) for value in values if is_scored(value)]
    return sum(scored) / len(scored) if scored else None


def _clean(value: float | None) -> float | None:
    """``NaN`` → ``None``。JSON 里的 ``NaN`` 不是合法 JSON，下次对比会读不回来。"""

    return float(value) if is_scored(value) else None


def _block(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """一组题的 CP / CR 均值与各自的已评分条数。"""

    precisions = [score.context_precision for score in scores]
    recalls = [score.context_recall for score in scores]
    return {
        "context_precision": _clean(mean_scored(precisions)),
        "context_recall": _clean(mean_scored(recalls)),
        "scored": {
            "context_precision": sum(1 for value in precisions if is_scored(value)),
            "context_recall": sum(1 for value in recalls if is_scored(value)),
        },
        "total": len(scores),
    }


def score_run(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """把逐题结果折成「整体 + 分题型 + 排除统计 + 未评分清单」。

    ``absent`` / ``false_premise`` 在**这里**就被排除，且只排一次：报告、门禁、机读结果
    读的都是同一份 ``metrics``，不存在「报告按 131 算、门禁按 127 算」这种错位。
    """

    excluded = Counter(score.type for score in scores if score.type in EXCLUDED_QUESTION_TYPES)
    included = [score for score in scores if score.type not in EXCLUDED_QUESTION_TYPES]
    by_type = {
        type_name: _block([score for score in included if score.type == type_name])
        for type_name in sorted({score.type for score in included})
    }
    unscored = [
        {
            "id": score.id,
            "type": score.type,
            "context_precision": _clean(score.context_precision),
            "context_recall": _clean(score.context_recall),
            "reason": score.unscored_reason or "unscored",
        }
        for score in included
        if not (is_scored(score.context_precision) and is_scored(score.context_recall))
    ]
    return {
        "overall": _block(included),
        "by_type": by_type,
        "excluded": dict(sorted(excluded.items())),
        "unscored": unscored,
    }


# ─────────────────────────────────────────────── ragas ID 版双指标（零 LLM）


async def context_scores(
    retrieved_context_ids: Sequence[str], reference_context_ids: Sequence[str]
) -> tuple[float | None, float | None]:
    """用 ragas 的 **ID 版**指标算出 ``(context_precision, context_recall)``。

    只吃两串 chunk id：``retrieved`` 是这次真实检索返回的，``reference`` 是题目标注
    ``(document, heading_path)`` 解析出的真实切片 id（解析逻辑只有一份，在
    ``scripts/eval_retrieval.py``）。**不调任何模型**，所以门禁在「LLM 账单出问题的
    那一天」照样能跑（#98 用户故事 25）。

    口径（ragas 0.4.3 的语义，逐字如此，别在别处再实现一遍）：

    * CP = |retrieved ∩ reference| / |retrieved|（集合口径，同一片被两路召回只算一次）；
    * CR = |retrieved ∩ reference| / |reference|（标了多少、找到几成）。

    两个 NaN 处理要分开：返回空时 CP 没有分母（NaN）→ 记 ``None``（未评分）；
    CR 的分母是标注数，返回空是**真实的 0 分**，不能当成「没测」。
    """

    from ragas.dataset_schema import SingleTurnSample
    from ragas.metrics._context_precision import IDBasedContextPrecision
    from ragas.metrics._context_recall import IDBasedContextRecall

    # 私有导入是 ragas 0.4.3 里唯一可用入口（ID 版指标不在 ``__all__``），
    # ``tests/test_dependency_compat.py`` 守着「它还在、还是 Metric 子类」。
    sample = SingleTurnSample(
        retrieved_context_ids=[str(chunk_id) for chunk_id in retrieved_context_ids],
        reference_context_ids=[str(chunk_id) for chunk_id in reference_context_ids],
    )
    precision = await IDBasedContextPrecision().single_turn_ascore(sample)
    recall = await IDBasedContextRecall().single_turn_ascore(sample)
    return _clean(precision), _clean(recall)


# ───────────────────────────────────────────────────────────── 语料版本


def corpus_version(files: Mapping[str, str]) -> str:
    """语料版本：对「篇名 + 正文」的内容寻址哈希，取前 12 位。

    为什么不用时间戳、不用切片数：两者都会在「语料没变、只是重新索引了一次」时变化，
    而这一栏要回答的是「这次和基线是不是同一份语料」。内容没变 → 版本不变 → 可以比；
    改了一个字 → 版本变 → 判为不可比，免得拿两份不同语料的分数谈「退化了多少」。
    """

    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[name].encode("utf-8")).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


# ───────────────────────────────────────────────────── 数据集校验（离线）


def _locator(label: Mapping[str, Any]) -> tuple[str, str]:
    return (str(label.get("document", "")), str(label.get("heading_path", "")))


def validate_dataset(
    payload: Mapping[str, Any],
    *,
    locators: Mapping[tuple[str, str], int],
    top_k_max: int = 50,
) -> dict[str, Any]:
    """题库的离线校验：标注能不能在**真实切块**里定位、题型与排除规则自不自洽。

    ``locators`` 是 ``(文档 stem, heading_path) → 片数``——由调用方用真实切块器算出来
    （离线，不连库）。「0 片必须当场失败」这条规则与 ``scripts/eval_retrieval.py``
    的标签解析同源：标注写错时要报「找不到」，不能静默算成漏召。

    返回 ``{"problems": [...], "warnings": [...], "stats": {...}}``：``problems`` 非空
    即退出码 1；``warnings`` 只提示（例如同一道题被写了两遍）。
    """

    problems: list[str] = []
    warnings: list[str] = []
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        return {
            "problems": ["questions 为空：题库里一道题都没有"],
            "warnings": [],
            "stats": {"questions": 0, "answerable": 0, "gate": 0, "labels": 0, "excluded": {}},
        }

    k = payload.get("k")
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= top_k_max:
        problems.append(f"k 必须是 1..{top_k_max} 的整数，实际 {k!r}")

    seen_ids: set[str] = set()
    excluded: Counter[str] = Counter()
    labels_total = 0
    answerable = 0
    gate_count = 0
    seen_questions: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {}

    for item in questions:
        qid = str(item.get("id") or "")
        if not qid:
            problems.append("有一道题没有 id")
            continue
        if qid in seen_ids:
            problems.append(f"{qid}: id 重复")
        seen_ids.add(qid)

        type_name = str(item.get("type") or "")
        if not type_name:
            # 题型不是装饰：分题型拆解、排除规则、报告都靠它。
            problems.append(f"{qid}: 缺 type 字段")
        query = str(item.get("query") or "")
        if not query.strip():
            problems.append(f"{qid}: query 为空")

        relevant = list(item.get("relevant") or [])
        traps = list(item.get("trap") or [])
        if type_name == "absent":
            if relevant:
                problems.append(f"{qid}: absent 题不该有标注（它的正确答案是一条都不返回）")
        elif not relevant:
            problems.append(f"{qid}: 非 absent 题必须有至少一条标注")
        else:
            answerable += 1
            if type_name not in EXCLUDED_QUESTION_TYPES:
                gate_count += 1
        if type_name in EXCLUDED_QUESTION_TYPES:
            excluded[type_name] += 1

        relevant_locators = [_locator(label) for label in relevant]
        trap_locators = [_locator(label) for label in traps]
        for field_name, entries in (("标注", relevant_locators), ("陷阱", trap_locators)):
            for document, heading_path in entries:
                if not document or not heading_path:
                    problems.append(f"{qid}: {field_name}缺 document / heading_path")
                    continue
                if locators.get((document, heading_path), 0) == 0:
                    problems.append(
                        f"{qid}: {field_name}找不到切片 -> document={document} "
                        f"heading_path={heading_path!r}"
                    )
        labels_total += len(relevant_locators)
        for trap in trap_locators:
            if trap in relevant_locators:
                problems.append(f"{qid}: 陷阱与金标指向同一节 {trap[0]} > {trap[1]}")

        # 同一句问句 + 同一组标注出现两次 = 这道题在平均里被记了两遍。
        # 这是**警告**不是错误：题库里真有一对（q24 / q66）共享同一句问句与同一节金标，
        # 它让 rare_token 这一类暗中加权。改题库属于题库的票，这里只把事实说出来。
        signature = (query, tuple(relevant_locators))
        if query and signature in seen_questions:
            warnings.append(
                f"{qid} 与 {seen_questions[signature]} 是同一句问句 + 同一组标注，"
                "这一类题在平均里会被记两遍"
            )
        seen_questions.setdefault(signature, qid)

    return {
        "problems": problems,
        "warnings": warnings,
        "stats": {
            "questions": len(questions),
            "answerable": answerable,
            "gate": gate_count,
            "labels": labels_total,
            "excluded": dict(sorted(excluded.items())),
        },
    }


# ─────────────────────────────────────────────────── 实验配置校验


_EXPERIMENT_KEYS = frozenset({"name", "mode", "top_k", "split_strategy"})
_GATE_KEYS = frozenset({"overall_pp", "per_type_pp", "baseline"})
_HTTP_KEYS = frozenset({"samples", "base_url", "endpoint"})


def validate_experiment(
    config: Mapping[str, Any],
    *,
    modes: Sequence[str],
    strategies: Sequence[str],
    top_k_max: int = 50,
) -> dict[str, Any]:
    """实验配置的校验：键名、取值、以及两条判据之间不能自相矛盾。

    「实验条件」全在一份文件里（``eval/experiment.toml``），一次跑的条件因此可被复核、
    可被 diff。未知键一律拒绝——拼错的键会被悄悄忽略，而人只看得到「我明明配了」。
    """

    problems: list[str] = []
    known_sections = {"experiment", "gate", "http"}
    for section in sorted(set(config) - known_sections):
        problems.append(f"未知配置段 [{section}]（只认 {', '.join(sorted(known_sections))}）")

    experiment = config.get("experiment") or {}
    gate = config.get("gate") or {}
    http = config.get("http") or {}
    for section_name, block, allowed in (
        ("experiment", experiment, _EXPERIMENT_KEYS),
        ("gate", gate, _GATE_KEYS),
        ("http", http, _HTTP_KEYS),
    ):
        if not isinstance(block, Mapping):
            problems.append(f"[{section_name}] 必须是一个表")
            continue
        for key in sorted(set(block) - allowed):
            problems.append(f"未知键 [{section_name}].{key}")

    name = experiment.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("[experiment].name 必须是非空字符串")

    mode = experiment.get("mode")
    if mode not in modes:
        problems.append(f"[experiment].mode={mode!r} 不是允许的模式（{', '.join(modes)}）")
    top_k = experiment.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= top_k_max:
        problems.append(f"[experiment].top_k 必须是 1..{top_k_max} 的整数，实际 {top_k!r}")
    strategy = experiment.get("split_strategy")
    if strategy not in strategies:
        problems.append(
            f"[experiment].split_strategy={strategy!r} 不是允许的策略（{', '.join(strategies)}）"
            "——本运行器按 heading_path 解析标注，只有 structure 切得出章节路径"
        )

    overall_pp = gate.get("overall_pp")
    per_type_pp = gate.get("per_type_pp")
    for key, value in (("overall_pp", overall_pp), ("per_type_pp", per_type_pp)):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            problems.append(f"[gate].{key} 必须是正数（百分点），实际 {value!r}")
    if (
        isinstance(overall_pp, (int, float))
        and isinstance(per_type_pp, (int, float))
        and not isinstance(overall_pp, bool)
        and not isinstance(per_type_pp, bool)
        and per_type_pp < overall_pp
    ):
        problems.append(
            f"[gate].per_type_pp={per_type_pp} 小于 overall_pp={overall_pp}："
            "单题型阈值必须**更松**，否则同一处退化会既过又不过"
        )
    baseline = gate.get("baseline")
    if baseline is not None and not isinstance(baseline, str):
        problems.append("[gate].baseline 必须是字符串（空串 = 自动挑基线）")

    samples = http.get("samples")
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 1:
        problems.append(f"[http].samples 必须是 >=1 的整数，实际 {samples!r}")
    for key in ("base_url", "endpoint"):
        value = http.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"[http].{key} 必须是字符串")

    return {
        "problems": problems,
        "resolved": {
            "name": name,
            "mode": mode,
            "top_k": top_k,
            "split_strategy": strategy,
            "overall_pp": float(overall_pp) if isinstance(overall_pp, (int, float)) else None,
            "per_type_pp": float(per_type_pp) if isinstance(per_type_pp, (int, float)) else None,
            "baseline": baseline or "",
            "http_samples": samples,
        },
    }


# ─────────────────────────────────────────────── 相对判据（整体 −1pp / 题型 3pp）


@dataclass(frozen=True)
class GateThresholds:
    """两条判据的阈值，单位是**百分点**（pp）。"""

    overall_pp: float = DEFAULT_OVERALL_PP
    per_type_pp: float = DEFAULT_PER_TYPE_PP

    def as_dict(self) -> dict[str, float]:
        return {"overall_pp": self.overall_pp, "per_type_pp": self.per_type_pp}


def pp_delta(current: float, baseline: float) -> float:
    """百分点差（``current - baseline`` 乘以 100），保留浮点原值供判据用。"""

    return (float(current) - float(baseline)) * 100.0


def _verdict(
    verdict: str,
    passed: bool | None,
    *,
    thresholds: GateThresholds,
    summary: str,
    reason: str | None = None,
    baseline: Mapping[str, Any] | None = None,
    overall: Sequence[Mapping[str, Any]] = (),
    by_type: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    regressions = [row for row in overall if row["status"] == "fail"] + [
        row for row in by_type if row["status"] == "fail"
    ]
    return {
        "verdict": verdict,
        "passed": passed,
        "reason": reason,
        "thresholds": thresholds.as_dict(),
        "baseline": dict(baseline) if baseline is not None else None,
        "overall": list(overall),
        "by_type": list(by_type),
        "regressions": regressions,
        "summary": summary,
    }


def _ruler(payload: Mapping[str, Any]) -> dict[str, Any]:
    """定义「这把尺子」的几个轴：任何一个变了，就不是同一个测量。"""

    environment = payload.get("environment") or {}
    experiment = payload.get("experiment") or {}
    chunking = environment.get("chunking") or {}
    embedding = environment.get("embedding") or {}
    return {
        "检索模式": experiment.get("mode"),
        "返回条数": experiment.get("top_k"),
        "切块策略": chunking.get("split_strategy"),
        "切块窗口": [chunking.get("chunk_size"), chunking.get("chunk_overlap")],
        "嵌入模型": [embedding.get("model"), embedding.get("dimensions")],
    }


def _comparability_problem(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> str | None:
    """两侧能不能比：先看是不是同一组题，再看是不是同一份语料、同一把尺子。"""

    current_ids = set((current.get("scope") or {}).get("question_ids") or [])
    baseline_ids = set((baseline.get("scope") or {}).get("question_ids") or [])
    if current_ids != baseline_ids:
        return (
            f"两侧题集不同（本次 {len(current_ids)} 道、基线 {len(baseline_ids)} 道）；"
            "只有同一组题上的差值才有意义——`--limit` 之类裁剪过的运行不进闸门"
        )
    current_version = (current.get("corpus") or {}).get("version")
    baseline_version = (baseline.get("corpus") or {}).get("version")
    if current_version != baseline_version:
        return (
            f"语料版本不同（本次 {current_version}、基线 {baseline_version}）；"
            "换语料后基线不再适用，请重新建立基线"
        )
    # 同一份语料、同一个题库，也可能因为换了切块窗口或嵌入模型而量出不同的数。
    # 那不是「检索变差」，是「换了把尺子」——差值没有意义，必须说成不可比。
    current_ruler = _ruler(current)
    baseline_ruler = _ruler(baseline)
    differing = [
        f"{axis}：本次 {current_ruler[axis]!r}、基线 {baseline_ruler[axis]!r}"
        for axis in current_ruler
        if current_ruler[axis] != baseline_ruler[axis]
    ]
    if differing:
        return "判定口径不同（" + "；".join(differing) + "）；换了尺子就不能谈「退化了多少」"
    return None


def compare_to_baseline(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    *,
    thresholds: GateThresholds = GateThresholds(),
    baseline_source: str | None = None,
) -> dict[str, Any]:
    """把本次结果与一份已归档的基线比，给出绿 / 红 / 无基线 / 不可比。

    判据（两条都必须满足）：

    * 整体：``context_precision`` 与 ``context_recall`` 都不低于基线 −``overall_pp``；
    * 分题型：每一类的同名指标都不低于基线 −``per_type_pp``。

    「没评上分」判红：一条都没测到却判绿，就是最坏的假绿。
    """

    if baseline is None:
        return _verdict(
            "no_baseline",
            None,
            thresholds=thresholds,
            summary=(
                "没有可比的基线：本次不判红，但它**不代表通过**——"
                "把这次归档成基线（见复现命令里的 --update-baseline），下一次才有闸门。"
            ),
            reason="no_baseline",
        )

    provenance = {
        "run_id": baseline.get("run_id"),
        "generated_at": baseline.get("generated_at"),
        "run_dir": baseline.get("run_dir"),
        "source": baseline_source,
    }

    problem = _comparability_problem(current, baseline)
    if problem is not None:
        return _verdict(
            "incomparable",
            None,
            thresholds=thresholds,
            summary=f"本次与基线不可比：{problem}。因此这里不给出任何「变好 / 变坏」的结论，也不参与门禁。",
            reason="incomparable",
            baseline=provenance,
        )

    current_overall = (current.get("metrics") or {}).get("overall") or {}
    baseline_overall = (baseline.get("metrics") or {}).get("overall") or {}

    overall_rows: list[dict[str, Any]] = []
    for metric in GATE_METRICS:
        now = current_overall.get(metric)
        before = baseline_overall.get(metric)
        row: dict[str, Any] = {
            "metric": metric,
            "current": _clean(now),
            "baseline": _clean(before),
            "delta_pp": None,
            "threshold_pp": thresholds.overall_pp,
            "status": "pass",
            "detail": "",
        }
        if not is_scored(now):
            row["status"] = "fail"
            row["detail"] = "本次一条都没评上分（未评分不计入平均）——没测到不等于没退化"
        elif not is_scored(before):
            row["status"] = "skipped"
            row["detail"] = "基线缺这个指标，无法比较"
        else:
            row["delta_pp"] = pp_delta(now, before)
            if row["delta_pp"] < -thresholds.overall_pp - PP_TOLERANCE:
                row["status"] = "fail"
        overall_rows.append(row)

    current_types = (current.get("metrics") or {}).get("by_type") or {}
    baseline_types = (baseline.get("metrics") or {}).get("by_type") or {}
    by_type_rows: list[dict[str, Any]] = []
    for type_name in sorted(set(current_types) | set(baseline_types)):
        for metric in GATE_METRICS:
            now_block = current_types.get(type_name) or {}
            before_block = baseline_types.get(type_name) or {}
            now = now_block.get(metric)
            before = before_block.get(metric)
            row = {
                "type": type_name,
                "metric": metric,
                "count": now_block.get("total"),
                "count_baseline": before_block.get("total"),
                "current": _clean(now),
                "baseline": _clean(before),
                "delta_pp": None,
                "threshold_pp": thresholds.per_type_pp,
                "status": "pass",
                "detail": "",
            }
            if not now_block:
                row["status"] = "skipped"
                row["detail"] = "这一题型只在基线里有，无法比较"
            elif not before_block:
                row["status"] = "skipped"
                row["detail"] = "这一题型不在基线里，无法比较"
            elif not is_scored(now):
                row["status"] = "fail"
                row["detail"] = "这一题型本次一条都没评上分——没测到不等于没退化"
            elif not is_scored(before):
                row["status"] = "skipped"
                row["detail"] = "基线缺这个指标，无法比较"
            else:
                row["delta_pp"] = pp_delta(now, before)
                if row["delta_pp"] < -thresholds.per_type_pp - PP_TOLERANCE:
                    row["status"] = "fail"
            by_type_rows.append(row)

    failed = any(row["status"] == "fail" for row in overall_rows) or any(
        row["status"] == "fail" for row in by_type_rows
    )
    if not failed:
        gate_count = current_overall.get("total")
        return _verdict(
            "pass",
            True,
            thresholds=thresholds,
            summary=(
                f"绿：整体与 {len(current_types)} 个题型全部在阈值内"
                f"（整体 ≥ -{thresholds.overall_pp:.2f}pp、单题型退化 ≤ {thresholds.per_type_pp:.2f}pp，"
                f"{gate_count} 道题参与平均）。"
            ),
            baseline=provenance,
            overall=overall_rows,
            by_type=by_type_rows,
        )

    parts: list[str] = []
    for row in overall_rows:
        if row["status"] != "fail":
            continue
        if row["delta_pp"] is None:
            parts.append(f"{row['metric']} 整体未评分（判据 ≥ -{thresholds.overall_pp:.2f}pp）")
        else:
            parts.append(
                f"{row['metric']} 整体 {row['delta_pp']:+.2f}pp"
                f"（判据 ≥ -{thresholds.overall_pp:.2f}pp）"
            )
    for row in by_type_rows:
        if row["status"] != "fail":
            continue
        if row["delta_pp"] is None:
            parts.append(
                f"{row['type']} 的 {row['metric']} 未评分（判据 ≥ -{thresholds.per_type_pp:.2f}pp）"
            )
        else:
            parts.append(
                f"{row['type']} 的 {row['metric']} {row['delta_pp']:+.2f}pp"
                f"（判据 ≥ -{thresholds.per_type_pp:.2f}pp）"
            )
    return _verdict(
        "fail",
        False,
        thresholds=thresholds,
        summary="红：" + "；".join(parts) + "。",
        reason="regression",
        baseline=provenance,
        overall=overall_rows,
        by_type=by_type_rows,
    )


# ─────────────────────────────────────────────────────── 机读结果的形状


def build_results_payload(
    *,
    run_id: str,
    run_dir: str,
    generated_at: str,
    status: str,
    experiment: Mapping[str, Any],
    corpus: Mapping[str, Any],
    environment: Mapping[str, Any],
    scores: Sequence[QuestionScore],
    gate: Mapping[str, Any],
    per_question: Sequence[Mapping[str, Any]],
    scope_full: bool,
    excluded_types: Mapping[str, int] | None = None,
    http_smoke: Mapping[str, Any] | None = None,
    failure: Mapping[str, Any] | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """一份 run 的机读结果：报告、门禁、下次对比都只读它。

    ``metrics`` 由 :func:`score_run` 算一次，报告与门禁共用同一份——两处各算一遍迟早
    会分叉（报告按 131 道、门禁按 127 道那种错位）。
    """

    metrics = score_run(scores)
    # 排除统计以**题库**为准：这次没跑 absent / false_premise（它们不进闸门），
    # 但那 10 道题确实在题库里——报告里必须说清「排除了哪些、各几道」，
    # 而不是因为它们没被评分就显示成「无排除」。
    if excluded_types:
        metrics["excluded"] = dict(sorted(excluded_types.items()))
    included_ids = [score.id for score in scores if score.type not in EXCLUDED_QUESTION_TYPES]
    scope = {
        "full": scope_full,
        "question_ids": included_ids,
        "question_count": len(included_ids),
        "dataset_questions": corpus.get("questions"),
        "excluded_types": dict(sorted(metrics["excluded"].items())),
        "scored": metrics["overall"]["scored"],
        "unscored": [item["id"] for item in metrics["unscored"]],
    }
    return {
        "schema": _RUN_SCHEMA,
        "run_id": run_id,
        "run_dir": run_dir,
        "generated_at": generated_at,
        "status": status,
        "failure": dict(failure) if failure else None,
        "duration_seconds": duration_seconds,
        "experiment": dict(experiment),
        "corpus": dict(corpus),
        "environment": dict(environment),
        "scope": scope,
        "metrics": metrics,
        "per_question": [dict(item) for item in per_question],
        "gate": dict(gate),
        "http_smoke": dict(http_smoke) if http_smoke else None,
    }


# ─────────────────────────────────────────────────────────── 报告渲染


_VERDICT_LABELS = {
    "pass": "绿（通过）",
    "fail": "红（未通过）",
    "no_baseline": "无基线（本次不判红，但也不代表通过）",
    "incomparable": "不可比（本次不参与门禁）",
}


def _num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def _pp(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f}"


def _command(run_dir: str, key: str) -> str:
    return f"{RUNNER_COMMAND} report --run {run_dir} --select {key}"


def _cell(text: Any) -> str:
    """表格单元格：竖线会把 markdown 表格切坏。"""

    return str(text).replace("|", "\\|")


def _gate_section(payload: Mapping[str, Any]) -> list[str]:
    gate = payload.get("gate") or {}
    verdict = str(gate.get("verdict") or "no_baseline")
    run_dir = str(payload.get("run_dir") or f"eval/runs/{payload.get('run_id')}")
    thresholds = gate.get("thresholds") or {}
    overall_pp = float(thresholds.get("overall_pp", DEFAULT_OVERALL_PP))
    per_type_pp = float(thresholds.get("per_type_pp", DEFAULT_PER_TYPE_PP))
    metrics = payload.get("metrics") or {}
    overall = metrics.get("overall") or {}

    lines = ["## 门禁判定", "", f"**{_VERDICT_LABELS.get(verdict, verdict)}**", ""]
    lines.append(
        f"- 判据：整体不低于基线 −{overall_pp:.2f}pp；单题型退化不超过 {per_type_pp:.2f}pp。"
    )
    lines.append(f"- 结论：{gate.get('summary') or '（无）'}")
    baseline = gate.get("baseline") or {}
    if baseline:
        source = baseline.get("source") or baseline.get("run_dir") or "未知"
        lines.append(
            f"- 基线：`{baseline.get('run_id')}`（来源 `{source}`，"
            f"生成于 {baseline.get('generated_at') or '未知'}）"
        )
    lines.append(
        f"- 参与平均：{overall.get('total')} 道题；"
        f"已评分 CP {((overall.get('scored') or {}).get('context_precision'))}"
        f"/{overall.get('total')}、"
        f"CR {((overall.get('scored') or {}).get('context_recall'))}/{overall.get('total')}。"
    )
    lines.append(
        "- 口径：CP = 本次返回的**不同**切片里有多少在标注内（分母是返回条数），"
        "CR = 标注的切片被找回了几成（分母是标注条数）。两者都是**集合**口径，"
        "与 `eval/ACCEPTANCE.md` 里按行算的 P@5 / R@5 同量级、但不是同一把尺子——"
        "跨文档比较前先看分母。"
    )
    lines.append("")

    rows = {str(row["metric"]): row for row in (gate.get("overall") or [])}
    lines += [
        "| 指标 | 本次 | 基线 | Δ(pp) | 判据 | 结论 | 复现命令 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for metric in GATE_METRICS:
        row = rows.get(metric) or {
            "metric": metric,
            "current": overall.get(metric),
            "baseline": None,
            "delta_pp": None,
            "threshold_pp": overall_pp,
            "status": None,
            "detail": "",
        }
        key = f"metrics.overall.{metric}"
        lines.append(
            "| {metric} | {now} | {before} | {delta} | ≥ -{threshold:.2f}pp | {status} | `{cmd}` |".format(
                metric=metric,
                now=_num(row.get("current")),
                before=_num(row.get("baseline")),
                delta=_pp(row.get("delta_pp")),
                threshold=float(row.get("threshold_pp", overall_pp)),
                status=_status_label(row.get("status"), row.get("detail")),
                cmd=_cell(_command(run_dir, key)),
            )
        )
    if not rows:
        lines += [
            "",
            "（本次还没有可比基线，所以「Δ」与「结论」是空的；"
            "本次的 CP / CR 仍然照实列出——报告不因为还没基线就少一列数字。）",
        ]

    type_rows = gate.get("by_type") or []
    lines += [
        "",
        f"### 分题型（单类退化 > {per_type_pp:.0f}pp 判红；题数为本次进平均的题数）",
        "",
        "| 题型 | 题数 | CP | ΔCP(pp) | CR | ΔCR(pp) | 结论 | 复现命令 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    by_type_metrics = metrics.get("by_type") or {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in type_rows:
        grouped.setdefault(str(row["type"]), []).append(row)
    # 分题型拆解**常驻**报告：没有基线时也要把每一类的 CP / CR 列出来，
    # 否则「整体没退但某一类塌了」这件事在第一次跑里就看不见（#98 用户故事 29）。
    for type_name in sorted(set(by_type_metrics) | set(grouped)):
        group = grouped.get(type_name, [])
        cp_row = next((row for row in group if row["metric"] == "context_precision"), {})
        cr_row = next((row for row in group if row["metric"] == "context_recall"), {})
        block = by_type_metrics.get(type_name) or {}
        detail = "；".join(str(row.get("detail")) for row in group if row.get("detail"))
        statuses = {row.get("status") for row in group}
        status = (
            "fail"
            if "fail" in statuses
            else ("skipped" if "skipped" in statuses else ("pass" if group else None))
        )
        lines.append(
            "| {name} | {count} | {cp} | {dcp} | {cr} | {dcr} | {status} | `{cmd}` |".format(
                name=type_name,
                count=block.get("total", cp_row.get("count")),
                cp=_num(block.get("context_precision")),
                dcp=_pp(cp_row.get("delta_pp")),
                cr=_num(block.get("context_recall")),
                dcr=_pp(cr_row.get("delta_pp")),
                status=_status_label(status, detail),
                cmd=_cell(_command(run_dir, f"metrics.by_type.{type_name}")),
            )
        )
    lines.append("")
    return lines


def _status_label(status: Any, detail: Any = "") -> str:
    if status == "pass":
        return "通过"
    if status == "fail":
        return "红" + (f"（{detail}）" if detail else "")
    if status == "skipped":
        return "跳过" + (f"（{detail}）" if detail else "")
    return "—"


def _chunking_text(environment: Mapping[str, Any]) -> str:
    """切块窗口的一行描述；缺字段时说「未知」而不是编一个默认值。"""

    chunking = environment.get("chunking") or {}
    size = chunking.get("chunk_size")
    overlap = chunking.get("chunk_overlap")
    if size is None:
        return "窗口未知"
    return f"{size}（overlap {overlap}，表格行上限 {chunking.get('table_max_rows')}）"


def _scope_text(payload: Mapping[str, Any], overall: Mapping[str, Any]) -> str:
    """「进主平均几道」要说清分母：全量跑是 127 道，``--limit`` 跑是「5 / 127」。"""

    scope = payload.get("scope") or {}
    total = overall.get("total")
    gate_total = (payload.get("corpus") or {}).get("gate_questions")
    if scope.get("full") or gate_total in (None, total):
        return f"进主平均 {total} 道"
    return f"本次跑 {total} 道 / 题库进平均 {gate_total} 道"


def _environment_section(payload: Mapping[str, Any]) -> list[str]:
    run_dir = str(payload.get("run_dir") or f"eval/runs/{payload.get('run_id')}")
    environment = payload.get("environment") or {}
    retrieval = environment.get("retrieval") or {}
    embedding = environment.get("embedding") or {}
    keyword = environment.get("keyword") or {}
    corpus = payload.get("corpus") or {}
    metrics = payload.get("metrics") or {}
    overall = metrics.get("overall") or {}

    excluded_text = "、".join(
        f"{name} {count}" for name, count in (metrics.get("excluded") or {}).items()
    )
    switches = "、".join(
        f"{label} {'开' if retrieval.get(key) else '关'}"
        for label, key in (
            ("重排", "rerank_enabled"),
            ("改写", "query_rewrite_enabled"),
            ("同义词扩展", "synonym_expansion_enabled"),
        )
    )
    rows = [
        (
            "检索方式",
            "进程内 `retrieve()`（真实 PostgreSQL + Elasticsearch + 嵌入；不经 HTTP）",
            "environment.retrieval.mode",
        ),
        (
            "检索模式 / 返回条数",
            f"{retrieval.get('mode') or '未知'} / top_k={retrieval.get('top_k')}",
            "environment.retrieval",
        ),
        (
            "嵌入模型",
            f"{embedding.get('model') or '未知'}（{embedding.get('dimensions')} 维，"
            f"{embedding.get('provider') or '未知供应商'}）",
            "environment.embedding.model",
        ),
        (
            "关键词一路",
            f"{keyword.get('provider') or '未知'} / 索引 {keyword.get('index') or '未知'}",
            "environment.keyword.index",
        ),
        (
            "语料版本",
            f"`{corpus.get('version')}`：{corpus.get('documents')} 篇 / {corpus.get('chunks')} 片",
            "corpus.version",
        ),
        (
            "切块口径",
            f"{(environment.get('chunking') or {}).get('split_strategy') or '未知'}"
            f" / {_chunking_text(environment)}"
            "（这一行与语料版本一起决定「两次跑是不是同一把尺子」）",
            "environment.chunking",
        ),
        (
            "题库",
            f"{corpus.get('questions')} 道（{_scope_text(payload, overall)}；"
            f"排除 {excluded_text or '无'}）",
            "metrics.excluded",
        ),
        (
            "开关状态",
            switches or "未知",
            "environment.retrieval",
        ),
        (
            "门禁指标",
            "ragas ID 版 `context_precision` / `context_recall`（"
            "只吃 chunk id，零 LLM、零外部服务）",
            "environment.gate_metrics",
        ),
    ]
    lines = [
        "## 这次跑的是什么",
        "",
        "| 项 | 值 | 复现命令 |",
        "| --- | --- | --- |",
    ]
    for label, value, key in rows:
        lines.append(f"| {label} | {_cell(value)} | `{_cell(_command(run_dir, key))}` |")
    lines += [
        "",
        "完整环境快照（运行时 / git 提交 / 全部开关）见同目录的 `environment.json`。",
        "",
    ]
    return lines


def _unscored_section(payload: Mapping[str, Any]) -> list[str]:
    metrics = payload.get("metrics") or {}
    unscored = metrics.get("unscored") or []
    lines = ["## 未评分记录（不计入平均）", ""]
    if not unscored:
        lines += ["没有未评分记录：本次每一道进平均的题都算出了两个指标。", ""]
        return lines
    lines += [
        f"共 {len(unscored)} 条。**它们的指标不参与任何平均**——把「没测到」算成 0 分，"
        "会把一次环境故障变成一次「检索退化」。",
        "",
        "| 题号 | 题型 | CP | CR | 原因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in unscored:
        lines.append(
            f"| {item['id']} | {item['type']} | {_num(item.get('context_precision'))} | "
            f"{_num(item.get('context_recall'))} | {item.get('reason')} |"
        )
    lines.append("")
    return lines


def _per_question_section(payload: Mapping[str, Any]) -> list[str]:
    run_dir = str(payload.get("run_dir") or f"eval/runs/{payload.get('run_id')}")
    per_question = payload.get("per_question") or []
    lines = [
        "## 逐题明细",
        "",
        f"来源：`{_command(run_dir, 'per_question')}`；每一行是它的第 N 项，"
        f"`--select per_question.<N>`（0 起）可单独打印。",
        "",
        "| # | 题号 | 题型 | 返回 | 标注 | CP | CR | 未评分原因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(per_question):
        lines.append(
            f"| {index} | {item.get('id')} | {item.get('type')} | "
            f"{item.get('returned_count')} | {item.get('reference_count')} | "
            f"{_num(item.get('context_precision'))} | {_num(item.get('context_recall'))} | "
            f"{item.get('unscored_reason') or '—'} |"
        )
    lines.append("")
    return lines


def _http_smoke_section(payload: Mapping[str, Any]) -> list[str]:
    smoke = payload.get("http_smoke")
    if not smoke:
        return []
    lines = [
        "## HTTP 冒烟（不参与门禁）",
        "",
        f"- 目标：`{smoke.get('base_url')}` 的 `{smoke.get('endpoint')}`；"
        f"样本 {len(smoke.get('samples') or [])} 道。",
        f"- 结论：{'字段齐全' if smoke.get('passed') else '**字段或状态异常**'}。"
        "它只回答「进进程内对、序列化之后还对不对」，分数不进闸门。",
        "",
        "| 题号 | HTTP | code | 返回条数 | 缺哪些字段 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sample in smoke.get("samples") or []:
        missing = "、".join(sample.get("missing") or []) or "—"
        lines.append(
            f"| {sample.get('id')} | {sample.get('status_code')} | {sample.get('code')} | "
            f"{sample.get('returned')} | {missing} |"
        )
    lines.append("")
    return lines


def _reproduce_section(payload: Mapping[str, Any]) -> list[str]:
    run_dir = str(payload.get("run_dir") or f"eval/runs/{payload.get('run_id')}")
    experiment = payload.get("experiment") or {}
    limit = experiment.get("limit")
    extra = f" --limit {limit}" if limit else ""
    return [
        "## 复现命令（照抄即可）",
        "",
        "```powershell",
        '$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"',
        "uv sync --frozen",
        "# 1. 校验题库与实验配置（离线，不连库、不发网络）",
        f"{RUNNER_COMMAND} validate-dataset",
        f"{RUNNER_COMMAND} validate-config",
        "# 2. 真实跑（进程内检索；需要 PG / ES / 嵌入凭据）",
        f"{RUNNER_COMMAND} run --env-file <主 checkout>\\.env{extra}",
        "# 3. 重出这份报告（只读归档，不再跑检索）",
        f"{RUNNER_COMMAND} report --run {run_dir}",
        "# 4. 单独打印某一个数字（报告里每处数字旁边都写了这条命令）",
        f"{RUNNER_COMMAND} report --run {run_dir} --select metrics.overall.context_precision",
        "```",
        "",
        "上面每个数字都来自同一份 `results.json`：报告是它的渲染，不是另算一遍。",
        "",
    ]


def render_report(payload: Mapping[str, Any]) -> str:
    """把一份 run 的机读结果渲染成人读报告（含每个数字的复现命令）。

    状态是 ``failed`` 时**不渲染任何数字**：真实跑没跑成，摆一张空表只会让人误读成
    「结果是 0」。失败报告只写失败阶段、原因、以及下一步查什么。
    """

    run_id = str(payload.get("run_id") or "unknown")
    run_dir = str(payload.get("run_dir") or f"eval/runs/{run_id}")
    status = str(payload.get("status") or "ok")
    workspace = payload.get("scope") or {}
    full = bool(workspace.get("full", True))

    lines = [f"# 评测门禁报告 · {run_id}", ""]
    lines.append(f"- 生成时间：{payload.get('generated_at') or '未知'}")
    lines.append(f"- 归档目录：`{run_dir}`（`report.md` + `results.json` + `environment.json`）")
    if payload.get("promoted_from"):
        # 这份是「随仓库走的基线副本」：命令指向本目录，换一台机器照样能打印出同一个数字。
        lines.append(f"- 来源 run：`{payload.get('promoted_from')}`（本目录是它的基线副本）")
    if status == "failed":
        failure = payload.get("failure") or {}
        lines += [
            "- 状态：**未跑出数字**",
            "",
            "## 为什么没有数字",
            "",
            f"- 失败阶段：`{failure.get('stage') or '未知'}`",
            f"- 原因：{failure.get('message') or '未知'}",
            "",
            "这一次没有产出任何 CP / CR：**不存在就是不存在**，不会用占位数字顶上。"
            "修好之后重跑同一条命令即可；每一次跑都留一份带时间戳的档案，"
            "这份失败记录也会留着——它是「那一天为什么没有数字」的出处。",
            "",
        ]
        return "\n".join(lines)

    verdict = str((payload.get("gate") or {}).get("verdict") or "no_baseline")
    lines.append(f"- 状态：**{_VERDICT_LABELS.get(verdict, verdict)}**")
    if not full:
        lines.append(
            "- 范围：**`--limit` 裁剪过的运行，不参与门禁**（它的数字是真的，"
            "但只能用来验证通路，不能拿来判红绿）"
        )
    if payload.get("http_smoke"):
        lines.append("- HTTP 冒烟：见文末，**不参与门禁**")
    lines += ["", "> 门禁指标零 LLM 依赖：两个数只用 chunk id 与标注算，不需要裁判模型。", ""]

    lines += _environment_section(payload)
    lines += _gate_section(payload)
    lines += _unscored_section(payload)
    lines += _per_question_section(payload)
    lines += _http_smoke_section(payload)
    lines += _reproduce_section(payload)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────── 取值（--select）


def select_value(payload: Mapping[str, Any], key: str) -> Any:
    """按 ``a.b.3.c`` 这样的路径取一个值；取不到就 ``KeyError``（不静默返回 None）。

    ``--select`` 是报告里每个数字的「复现命令」的落点：路径写错时必须报错，否则
    一条打印 ``None`` 的命令看起来像「这个数字本来就是空的」。
    """

    if not key:
        raise KeyError("select 路径为空")
    node: Any = payload
    walked: list[str] = []
    for part in key.split("."):
        walked.append(part)
        if isinstance(node, Mapping):
            if part not in node:
                raise KeyError(f"没有这个键：{'.'.join(walked)}")
            node = node[part]
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            try:
                index = int(part)
            except ValueError as exc:
                raise KeyError(f"{'.'.join(walked[:-1])} 是列表，只能按下标取") from exc
            if not 0 <= index < len(node):
                raise KeyError(f"下标越界：{'.'.join(walked)}")
            node = node[index]
        else:
            raise KeyError(f"{'.'.join(walked[:-1])} 不是可下钻的对象")
    return node
