"""#159 第一组：切片编排**开 / 关**——同题、同参，只差 ``orchestration``（外加一臂诊断）。

> ⚠️ **归档脚本（ADR-0029 / #138 之后不可再跑）**。切片编排这个能力已经被撤销：服务、
> 三层开关、响应里的那一块、部署预算全部删掉了，所以本脚本的 ``--orchestration on``
> 两臂今天既打不开开关、也读不到响应里的块。**它的读数留在 ``eval/reports/t159/``**
> （ADR-0029 明说那批测量不删，它们是「这个能力不值得留」的证据），本文件只作为
> 「那些数字是怎么算出来的」的留档。要重新测这个能力，得先按 ADR-0029 的翻案条件翻案。

```text
题集 A：eval/refs-questions.json 的 8 道（引用语料，tenant_c6）
题集 B：137 题基线里 12 道 cross_section（跨节 = 按定义多要点，tenant_demo）
        × 三臂：
          off            不编排（对照基线）
          on_deploy      编排开着，**整步预算按部署值**（settings.ORCHESTRATION_TIMEOUT_MS）
          on_diagnostic  编排开着，整步预算放大到 30s（诊断臂，见下）
```

## 为什么需要第三臂（这不是「调参让数字好看」）

实测（本机 / 生产模型链接到的那一条 ``deepseek-v4-1-flash-economy``）：**一次**结构化调用
要 4.5～11 s，而编排「要点 + 角色」是**两次**调用、共用 ``ORCHESTRATION_TIMEOUT_MS`` 这一个
整步预算（部署值 4000 ms）。于是部署口径下编排必然 ``TimeoutError`` → 100% 降级 → 一块都没有，
「材料量变没变 / 完整度判得准不准」根本没有读数。

把这件事**当成读数**记下来（那是本组第一个结论：这个部署口径下编排等于不可用），再另跑一臂
把整步预算按服务的入参放大到 30 s，用来回答**能力本身**的问题——材料量与完整度那几栏只在
这一臂里有读数。两臂的区别只有一个数（预算），两侧（off / on）的区别也只有一个布尔；报告里
每一栏都标明它来自哪一臂，**不并排、不混算**。

## 其余参数（三臂逐项相同）

``mode=hybrid``、``top_k=5``、折叠开、表格行折回开、改写关、**重排关**（与 137 题基线同一
口径；重排是「同一台机器上的今天」的一部分，本组问的是编排）。关侧重排反而让「只差一个
布尔」更干净：三臂之间没有别的共享可变旋钮。

## 读数从哪来（不是按开关推断）

* 材料 = 响应的 ``retrieved_chunks``（折叠后的交付单元），**off 臂也有**；
* 要点 / 核心片 / 辅助片 / 完整度 = 响应的 ``orchestration`` 块，**off 臂这个键连存在都没有**；
* 降级 = ``metadata.retrieval.degraded_reason``（``orchestration failed`` 是那道专码）；
* 模型调用：检索侧每题 1 次嵌入（重排关）；编排那两次由块是否存在数出来——块在 = 2 次，
  降级 = 1~2 次**不可知**（响应不记编排内部的调用次数，这里不猜）。

## 预算

开跑前打印预估（三臂合计），并落进产物。本组预计 = 暖机 8 + off 20 + on_deploy 20（+≤40
次被取消的编排调用）+ on_diagnostic 60。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 以 ``python scripts/...`` 方式运行时补上仓库根
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPORT_DIR = REPO_ROOT / "eval" / "reports" / "t159"

#: 从 137 题基线里挑多少道（票面数字）。挑选规则写死，重跑选同一批。
GATE_LIMIT = 12
#: 「有明确多要点」的题型：跨节题的答案按定义横跨 ≥2 个节。
GATE_TYPE = "cross_section"
#: 三臂的名字。顺序固定在报告、产物、逐题表里。
ARM_OFF = "off"
ARM_DEPLOY = "on_deploy"
ARM_DIAGNOSTIC = "on_diagnostic"
ARMS = (ARM_OFF, ARM_DEPLOY, ARM_DIAGNOSTIC)
ON_ARMS = (ARM_DEPLOY, ARM_DIAGNOSTIC)
#: 诊断臂的整步预算：只为让「两次结构化调用」有机会跑完（实测 4.5–11 s/次）。它**不是**
#: 部署值，报告里每一栏都会标明来自这一臂。
DIAGNOSTIC_TIMEOUT_MS = 30_000
#: 重跑预算：诊断臂里仍有题在 30 s 内跑不完（端点长尾，实测单次 5~25 s）。为了让
#: 「材料量 / 完整度」有一个**覆盖全部题**的读数，把跑不完的那几道按 60 s 重跑一次。
#: 每个产物行都记着自己是哪一档预算跑出来的（``on_diagnostic_budget_ms``），不混算。
RETRY_TIMEOUT_MS = 60_000
#: 评估语料 / 引用语料各自的关键词别名（与本机既有的版本化配置一致，见 #157 / C6）。
GATE_KEYWORD_ALIAS = "rag_chunks_eval"
REFS_KEYWORD_ALIAS = "rag_chunks_c6"
#: 引用语料那个库的展示名（真 id 是 UUID，生产建库不接受调用方指定 id，只能按名查）。
REFS_KB_NAME = "引用语料库·会员权益"
REFS_TENANT = "tenant_c6"


# ────────────────────────────────────────────────────────── 纯函数（离线可测）


def select_gate_questions(questions: Sequence[Any], *, limit: int = GATE_LIMIT) -> list[Any]:
    """从 137 题里挑「有明确多要点」的题：同题型 + 标注横跨 ≥2 节，按题号顺序取前 ``limit`` 道。

    为什么两条都要求：``cross_section`` 说的是「答案横跨两节」，而标注横跨几节是那份题集里
    唯一**结构化**的多要点证据（题库没有 ``answer_points`` 字段）。只看题型不看标注，
    会把「名义跨节、实际只有一节」的题也算进来。
    """

    return [item for item in questions if item.type == GATE_TYPE and len(item.labels) >= 2][:limit]


def material_facts(outcome: Any, *, orchestration_enabled: bool) -> dict[str, Any]:
    """一道题一次运行的材料读数（**只读外部可观测量**）。

    编排块**原样读**：要点文本、核心 / 辅助片、完整度全部是服务端算出来的，这里不重算
    三态、也不自己判「够不够」——那是人工抽检的事（见 ``audit_block`` 与报告里的抽检节）。
    """

    block = getattr(outcome, "orchestration", None)
    materials = len(outcome.hits)
    material_chars = sum(len(hit.content) for hit in outcome.hits)
    facts: dict[str, Any] = {
        "materials": materials,
        "material_chars": material_chars,
        "points": None,
        "evidence": None,
        "primary": None,
        "supporting": None,
        "covered_materials": None,
        "unassigned_materials": None,
        "evidence_chars": None,
        "completeness": None,
        #: 编排那一步花了几次模型调用：``0``（关）/ ``2``（块在）/ ``None``（降级，不可知）。
        "orchestration_llm_calls": 0,
    }
    if block is None:
        if orchestration_enabled and getattr(outcome, "degraded_reason", None) is not None:
            # 降级时**至少**付了一次（要点调用），至多两次（第二次也发了）。不猜成 2。
            facts["orchestration_llm_calls"] = None
        return facts

    evidence = [dict(item) for item in (block.get("evidence") or [])]
    distinct: dict[str, dict[str, Any]] = {}
    for item in evidence:
        chunk_id = str(item.get("chunk_id") or "")
        if chunk_id and chunk_id not in distinct:
            distinct[chunk_id] = item
    facts.update(
        {
            "points": len(block.get("points") or []),
            "evidence": len(evidence),
            "primary": sum(1 for item in evidence if item.get("role") == "primary"),
            "supporting": sum(1 for item in evidence if item.get("role") == "supporting"),
            "covered_materials": len(distinct),
            # 没被挂到任何要点上的交付单元：它仍然返回给了调用方，只是编排没给它位置。
            "unassigned_materials": materials - len(distinct),
            "evidence_chars": sum(
                len(str(item.get("content") or "")) for item in distinct.values()
            ),
            "completeness": block.get("completeness"),
            "orchestration_llm_calls": 2,
        }
    )
    return facts


def audit_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """把编排块整理成人工抽检好读的形状：要点清单 + 每个要点的核心片正文。

    **保留正文**是刻意的：抽检要回答「核心片是否真的覆盖了这个要点」，只看 ``chunk_id``
    等于让抽检回去再查一次库——那就不是抽检，是第二次实验。
    """

    points = [dict(item) for item in (block.get("points") or [])]
    evidence = [dict(item) for item in (block.get("evidence") or [])]
    return {
        "completeness": block.get("completeness"),
        "points": points,
        "primary": [
            {
                "evidence_id": item.get("evidence_id"),
                "point_id": item.get("point_id"),
                "chunk_id": item.get("chunk_id"),
                "document": item.get("title"),
                "heading_path": item.get("heading_path"),
                "content": item.get("content"),
            }
            for item in evidence
            if item.get("role") == "primary"
        ],
        "supporting": [
            {
                "evidence_id": item.get("evidence_id"),
                "point_id": item.get("point_id"),
                "chunk_id": item.get("chunk_id"),
                "heading_path": item.get("heading_path"),
            }
            for item in evidence
            if item.get("role") == "supporting"
        ],
    }


def compare_rows(
    outcomes_by_arm: Mapping[str, Sequence[Any]],
    *,
    extras: Mapping[str, Mapping[str, Any]] | None = None,
    arms: Sequence[str] = ARMS,
) -> list[dict[str, Any]]:
    """三臂逐题配对（**按题号**，不按位置）：同一行里放每臂的读数。

    ``extras`` 是题集特有的旁证（引用语料的期望节覆盖、137 题的 CP/CR）：它们不是编排
    读数，所以不进各臂的事实，而是平铺在一行上。
    """

    by_id = {arm: {outcome.id: outcome for outcome in outcomes_by_arm[arm]} for arm in arms}
    base = outcomes_by_arm[arms[0]]
    rows: list[dict[str, Any]] = []
    for outcome in base:
        if any(outcome.id not in by_id[arm] for arm in arms):
            continue
        row: dict[str, Any] = {
            "id": outcome.id,
            "query": outcome.query,
            "mechanism": outcome.mechanism,
        }
        for arm in arms:
            paired = by_id[arm][outcome.id]
            row[arm] = material_facts(paired, orchestration_enabled=arm != ARM_OFF)
            row[f"{arm}_latency_ms"] = paired.latency_ms
            row[f"{arm}_model_calls"] = paired.model_calls
            row[f"{arm}_degraded_reason"] = paired.degraded_reason
        row.update(dict((extras or {}).get(outcome.id) or {}))
        rows.append(row)
    return rows


def aggregate(rows: Sequence[Mapping[str, Any]], *, arms: Sequence[str] = ARMS) -> dict[str, Any]:
    """逐臂合计：材料量、要点数、完整度三态、降级率、调用与延迟。

    **完整度与降级率只对开编排的那两臂有意义**：off 臂没有块，把它的 ``None`` 混进三态
    分布会让分布分母变成「三臂题数」，看起来像「大部分题没有完整度」。
    """

    per_arm: dict[str, Any] = {}
    for arm in arms:
        facts = [row[arm] for row in rows]
        completeness = {"complete": 0, "partial": 0, "missing": 0}
        for item in facts:
            state = item.get("completeness")
            if state in completeness:
                completeness[state] += 1
        degraded = [
            row for row in rows if row.get(f"{arm}_degraded_reason") == "orchestration failed"
        ]
        points = [item["points"] for item in facts if item.get("points") is not None]
        per_arm[arm] = {
            "materials_total": sum(item["materials"] for item in facts),
            "materials_chars_total": sum(item["material_chars"] for item in facts),
            "assigned_materials_total": sum(
                item["covered_materials"]
                for item in facts
                if item.get("covered_materials") is not None
            ),
            "unassigned_materials_total": sum(
                item["unassigned_materials"]
                for item in facts
                if item.get("unassigned_materials") is not None
            ),
            "evidence_chars_total": sum(
                item["evidence_chars"] for item in facts if item.get("evidence_chars") is not None
            ),
            "points": {
                "total": sum(points),
                "questions_with_points": len(points),
                "avg": (sum(points) / len(points)) if points else None,
            },
            "completeness": completeness,
            "degraded": {
                "count": len(degraded),
                "rate": (len(degraded) / len(rows)) if rows else None,
                "ids": [row["id"] for row in degraded],
            },
            "retrieval_model_calls": sum(int(row.get(f"{arm}_model_calls") or 0) for row in rows),
            "orchestration_llm_calls_lower_bound": _orchestration_calls(facts),
            "latency": {
                "p50": _percentile([row[f"{arm}_latency_ms"] for row in rows], 0.50),
                "p95": _percentile([row[f"{arm}_latency_ms"] for row in rows], 0.95),
            },
        }
    return {
        "questions": len(rows),
        "arms": per_arm,
        "degraded_by_arm": {arm: per_arm[arm]["degraded"]["count"] for arm in arms},
        "completeness_by_arm": {arm: per_arm[arm]["completeness"] for arm in ON_ARMS},
    }


def _orchestration_calls(facts: Sequence[Mapping[str, Any]]) -> int:
    """编排调用数的**下限**：块在记 2，降级记 1（第二次可能没发出去，不能猜成 2）。"""

    total = 0
    for item in facts:
        calls = item.get("orchestration_llm_calls")
        total += 1 if calls is None else int(calls)
    return total


def _percentile(values: Sequence[float], point: float) -> float | None:
    """最近秩百分位（与 ``eval_refs._percentile`` 同一算法；样本少时不做插值）。"""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(point * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 2)


def predicted_calls(
    *,
    refs_questions: int,
    gate_questions: int,
    rerank: bool = False,
    warmup_per_batch: int = 4,
    batches: int = 2,
) -> dict[str, Any]:
    """开跑前的预估：与实跑口径**同一套公式**，否则预估只是装饰。

    每个题集：检索暖机 1 次嵌入 + 编排暖机（1 嵌入 + 2 次结构化）；
    三臂各一次查询嵌入；两个开臂各再付 ≤2 次/题的结构化调用（被 4 s 预算取消的那次
    也可能已经发出去了，所以按 2 计上界）。
    """

    total_questions = refs_questions + gate_questions
    per_question = 1 + (1 if rerank else 0)
    retrieval = per_question * total_questions
    orchestration_upper = 2 * total_questions * len(ON_ARMS)
    off_calls = retrieval
    on_deploy_calls = retrieval + 2 * total_questions
    on_diagnostic_calls = retrieval + 2 * total_questions
    total = batches * warmup_per_batch + off_calls + on_deploy_calls + on_diagnostic_calls
    return {
        "question_count": total_questions,
        "off_calls": off_calls,
        "on_deploy_calls_upper": on_deploy_calls,
        "on_diagnostic_calls_upper": on_diagnostic_calls,
        "orchestration_upper": orchestration_upper,
        "total_upper": total,
        "assumptions": {
            "rerank": "on" if rerank else "off",
            "per_question_retrieval_calls": per_question,
            "orchestration_calls_per_question_upper": 2,
            "warmup_per_batch": warmup_per_batch,
            "batches": batches,
        },
        "note": (
            "预估是**上界**：部署臂那两次结构化调用会被 4 s 预算取消，取消前可能已发出，"
            "所以按 2 计。"
        ),
    }


# ─────────────────────────────────────────────────────────────── 真实跑批


def _apply_batch_settings(
    *, keyword_alias: str, rerank: bool, orchestration_timeout_ms: int
) -> None:
    """把这一组评测要用的旋钮钉死（与既有评测脚本同一条纪律：不吃 .env 的偶然值）。"""

    from app.core.config import settings

    settings.ELASTICSEARCH_INDEX = keyword_alias
    settings.RERANK_ENABLED = bool(rerank)
    settings.QUERY_REWRITE_ENABLED = False
    settings.ORCHESTRATION_TIMEOUT_MS = int(orchestration_timeout_ms)


async def _refs_kb_id() -> str:
    """按展示名查引用语料那个库（生产建库不接受调用方指定 id，见 manifest 说明）。"""

    from sqlalchemy import select

    from app.db.async_session import AsyncSessionLocal
    from app.models.knowledge_base import KnowledgeBase

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == REFS_TENANT,
                    KnowledgeBase.name == REFS_KB_NAME,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise SystemExit(
            f"找不到引用语料库（{REFS_TENANT} / {REFS_KB_NAME}）：它的准备脚本 "
            "scripts/prep_eval_c6.py 只活在 tag `step11-with-graph-and-orchestration`"
            "（本分支已随库级索引版本层整层删除；本脚本本身也是归档脚本，要复跑先回到那个 tag）。"
        )
    return str(row.id)


def _gate_kb_ids() -> tuple[str, ...]:
    """137 题那套评测库（清单里的每一个库）。"""

    from scripts import eval_retrieval

    layouts, _empty = eval_retrieval._load_manifest()
    return tuple(layout.kb_id for layout in layouts)


async def _run_arm(
    questions: Sequence[Mapping[str, Any]],
    *,
    kb_ids: Sequence[str],
    tenant_id: str,
    orchestration: bool,
) -> list[Any]:
    """跑一臂（``orchestration`` 是唯一的差别；预算由进程设置决定）。"""

    from scripts import eval_refs

    return await eval_refs.run_live(
        questions=questions,
        kb_ids=kb_ids,
        top_k=5,
        tenant_id=tenant_id,
        graph="off",
        research="off",
        rerank=eval_refs.RERANK_OFF,
        orchestration=orchestration,
    )


async def _warm_up(
    *,
    kb_ids: Sequence[str],
    tenant_id: str,
    label: str,
    orchestration_timeout_ms: int,
) -> dict[str, Any]:
    """计分之前把两种冷启动都付掉（#157 的纪律，扩展到模型链）：

    1. 检索：第一次检索会装配可观测性客户端并堵住事件循环（关键词一路因此超时）；
    2. 模型链：第一次编排要装配 langchain/openai 客户端，实测那次要 11 s，而整步预算
       只有 30 s——不暖机的话，第一道计分题会替整条链的冷启动买单。
    """

    from scripts import eval_refs

    _apply_batch_settings(
        keyword_alias=_current_alias(),
        rerank=False,
        orchestration_timeout_ms=orchestration_timeout_ms,
    )
    retrieval = (
        await eval_refs.run_live(
            questions=[
                {"id": "warmup-retrieval", "query": "证书过期怎么处理", "mechanism": "warmup"}
            ],
            kb_ids=kb_ids,
            top_k=5,
            tenant_id=tenant_id,
            graph="off",
            research="off",
            rerank=eval_refs.RERANK_OFF,
        )
    )[0]
    orchestration = (
        await eval_refs.run_live(
            questions=[
                {
                    "id": "warmup-orchestration",
                    "query": "退款要几天，从哪个入口申请",
                    "mechanism": "warmup",
                }
            ],
            kb_ids=kb_ids,
            top_k=5,
            tenant_id=tenant_id,
            graph="off",
            research="off",
            rerank=eval_refs.RERANK_OFF,
            orchestration=True,
        )
    )[0]
    return {
        "batch": label,
        "retrieval": {
            "latency_ms": retrieval.latency_ms,
            "degraded_reason": retrieval.degraded_reason,
            "model_calls": retrieval.model_calls,
        },
        "orchestration": {
            "latency_ms": orchestration.latency_ms,
            "degraded_reason": orchestration.degraded_reason,
            "has_block": orchestration.orchestration is not None,
            "model_calls": 2 if orchestration.orchestration is not None else 1,
        },
    }


def _current_alias() -> str:
    from app.core.config import settings

    return str(settings.ELASTICSEARCH_INDEX)


async def _gate_scores(*, questions: Sequence[Any], outcomes: Sequence[Any]) -> dict[str, Any]:
    """137 题那 12 道的 CP / CR（与 137 题门禁**同一个尺子**：ragas ID 版，零 LLM 打分）。

    标签按 ``(document, heading_path)`` 解析（#157 的纪律），不按 ``chunk_id``。
    """

    from scripts import eval_c6_guard, eval_retrieval, eval_runner

    chunks = await eval_c6_guard._load_indexed_chunks()
    labels, _traps = eval_retrieval._resolve_labels(list(questions), chunks)
    scored: dict[str, Any] = {}
    for outcome in outcomes:
        reference = [str(chunk_id) for chunk_id in labels.get(outcome.id, [])]
        returned = [hit.chunk_id for hit in outcome.hits if hit.chunk_id]
        precision, recall = await eval_runner.context_scores(returned, reference)
        scored[outcome.id] = {
            "reference_count": len(reference),
            "context_precision": precision,
            "context_recall": recall,
        }
    return scored


async def _run_batches(*, rerank: bool) -> dict[str, Any]:
    """两个题集 × 三臂 + 暖机 + 打分。返回机读产物（``results.json`` 的完整载荷）。"""

    from app.core.config import settings
    from scripts import eval_refs, eval_retrieval

    # 部署值：先读下来记进产物，之后才允许 diagnostic 臂覆盖它。
    deployed_timeout_ms = int(settings.ORCHESTRATION_TIMEOUT_MS)

    refs_payload = eval_refs.load_questions()
    refs_questions = [
        {"id": item["id"], "query": item["query"], "mechanism": item["mechanism"]}
        for item in refs_payload["questions"]
    ]
    refs_source = refs_payload["questions"]

    gate_all, _k = eval_retrieval._load_questions()
    gate_selected = select_gate_questions(gate_all)
    gate_questions = [
        {"id": item.id, "query": item.query, "mechanism": item.type} for item in gate_selected
    ]

    model_chain = await eval_refs._bind_model_chain()

    batches: dict[str, Any] = {}
    gate_scores: dict[str, dict[str, Any]] = {}

    for name, questions, kb_ids, tenant_id, alias in (
        ("refs", refs_questions, None, REFS_TENANT, REFS_KEYWORD_ALIAS),
        ("gate", gate_questions, None, settings.DEV_TENANT_ID, GATE_KEYWORD_ALIAS),
    ):
        if name == "refs":
            kb_ids = [await _refs_kb_id()]
        else:
            kb_ids = list(_gate_kb_ids())

        # 部署臂：整步预算就是部署值。
        _apply_batch_settings(
            keyword_alias=alias, rerank=rerank, orchestration_timeout_ms=deployed_timeout_ms
        )
        warmup_deploy = await _warm_up(
            kb_ids=kb_ids,
            tenant_id=tenant_id,
            label=f"{name}:deploy",
            orchestration_timeout_ms=deployed_timeout_ms,
        )
        by_arm: dict[str, list[Any]] = {}
        by_arm[ARM_OFF] = await _run_arm(
            questions, kb_ids=kb_ids, tenant_id=tenant_id, orchestration=False
        )
        by_arm[ARM_DEPLOY] = await _run_arm(
            questions, kb_ids=kb_ids, tenant_id=tenant_id, orchestration=True
        )

        # 诊断臂：整步预算放大到 30s（只为让两次调用跑完；报告里每栏标明来源）。
        _apply_batch_settings(
            keyword_alias=alias, rerank=rerank, orchestration_timeout_ms=DIAGNOSTIC_TIMEOUT_MS
        )
        warmup_diagnostic = await _warm_up(
            kb_ids=kb_ids,
            tenant_id=tenant_id,
            label=f"{name}:diagnostic",
            orchestration_timeout_ms=DIAGNOSTIC_TIMEOUT_MS,
        )
        by_arm[ARM_DIAGNOSTIC] = await _run_arm(
            questions, kb_ids=kb_ids, tenant_id=tenant_id, orchestration=True
        )

        if name == "refs":
            coverage = {
                arm: eval_refs.coverage_by_question(by_arm[arm], refs_source) for arm in ARMS
            }
            extras = {
                qid: {
                    "coverage_off": coverage[ARM_OFF][qid],
                    "coverage_on_deploy": coverage[ARM_DEPLOY][qid],
                    "coverage_on_diagnostic": coverage[ARM_DIAGNOSTIC][qid],
                }
                for qid in coverage[ARM_OFF]
            }
        else:
            gate_scores = {
                arm: await _gate_scores(questions=gate_selected, outcomes=by_arm[arm])
                for arm in ARMS
            }
            extras = {
                qid: {
                    "reference_count": gate_scores[ARM_OFF][qid]["reference_count"],
                    **{
                        f"{key}_{arm}": gate_scores[arm][qid][key]
                        for arm in ARMS
                        for key in ("context_precision", "context_recall")
                    },
                }
                for qid in gate_scores[ARM_OFF]
            }
        rows = compare_rows(by_arm, extras=extras)
        blocks = {
            outcome.id: audit_block(outcome.orchestration) if outcome.orchestration else None
            for outcome in by_arm[ARM_DIAGNOSTIC]
        }
        for row in rows:
            row["on_diagnostic_budget_ms"] = DIAGNOSTIC_TIMEOUT_MS
            # 抽检要回答「核心片是否真的覆盖了这个要点」，所以证据正文必须进产物。
            row["on_diagnostic_block"] = blocks.get(row["id"])
        batches[name] = {
            "kb_ids": kb_ids,
            "tenant_id": tenant_id,
            "keyword_alias": alias,
            "questions": len(questions),
            "warmup": {"deploy": warmup_deploy, "diagnostic": warmup_diagnostic},
            "rows": rows,
            "aggregate": aggregate(rows),
        }

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "switches": {
            "mode": "hybrid",
            "top_k": 5,
            "fold_to_sections": True,
            "expand_table_rows": True,
            "rewrite": False,
            "rerank": "on" if rerank else "off",
            "orchestration": {"off": False, "on_deploy": True, "on_diagnostic": True},
            "orchestration_timeout_ms": {
                "deployed": deployed_timeout_ms,
                "diagnostic": DIAGNOSTIC_TIMEOUT_MS,
            },
        },
        "keyword_alias": {"refs": REFS_KEYWORD_ALIAS, "gate": GATE_KEYWORD_ALIAS},
        "llm_chain": model_chain,
        "premise": {
            "comparable_with_original_project": False,
            "why": (
                "本仓库的语料与题集都是自己的，原项目那组效果数字（+4.9% / +7.5% / −82.5% / "
                "+3.4pp）既不成立也不可比（#138 Out of Scope）。本轮数字只与本轮产物可比。"
            ),
        },
        "in_batches": batches,
    }


# ─────────────────────────────────────────────────────────────── 落盘与入口


def write_results(payload: Mapping[str, Any], *, out_dir: Path | None = None) -> Path:
    root = out_dir or (REPORT_DIR / f"run-orchestration-{payload['run_id']}")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "results.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _selection_count() -> tuple[int, int]:
    """题数（题集 A / B）。桥接两个题集，纯读文件，不连库。"""

    from scripts import eval_refs, eval_retrieval

    refs_questions = len(eval_refs.load_questions()["questions"])
    gate_all, _k = eval_retrieval._load_questions()
    return refs_questions, len(select_gate_questions(gate_all))


def _accounted_calls(payload: Mapping[str, Any]) -> dict[str, Any]:
    """实跑调用数（记账）：暖机 + 三臂检索 + 编排（降级处按**下限 1 次**记，不猜 2）。"""

    warmup = 0
    for batch in payload["in_batches"].values():
        for item in batch["warmup"].values():
            warmup += int(item["retrieval"].get("model_calls") or 0)
            warmup += int(item["orchestration"].get("model_calls") or 0)
        # 两个补跑口（诊断臂重跑 / 只补缺块）的暖机也要记账，否则「实耗」会少算。
        for key in ("retry", "diagnostic_refill"):
            entry = (batch.get(key) or {}).get("warmup")
            if isinstance(entry, Mapping):
                warmup += int((entry.get("retrieval") or {}).get("model_calls") or 0)
                warmup += int((entry.get("orchestration") or {}).get("model_calls") or 0)
    retrieval = sum(
        batch["aggregate"]["arms"][arm]["retrieval_model_calls"]
        for batch in payload["in_batches"].values()
        for arm in ARMS
    )
    orchestration_lower = sum(
        batch["aggregate"]["arms"][arm]["orchestration_llm_calls_lower_bound"]
        for batch in payload["in_batches"].values()
        for arm in ON_ARMS
    )
    return {
        "warmup": warmup,
        "retrieval": retrieval,
        "orchestration_lower_bound": orchestration_lower,
        "total_lower_bound": warmup + retrieval + orchestration_lower,
        # 降级处最多再多一次（第二次调用可能也发出去了）。
        "total_upper_bound": warmup
        + retrieval
        + orchestration_lower
        + sum(
            1
            for batch in payload["in_batches"].values()
            for row in batch["rows"]
            for arm in ON_ARMS
            if row[arm].get("orchestration_llm_calls") is None
        ),
    }


# ───────────────────────────────────────── 诊断臂补齐：换一档预算把这段再跑一次（并留正文）

#: 诊断臂里「没有块」这回事有两种来法，产物里分开记：第一次按 30 s 跑、第二次按 60 s 跑。
#: 报告里两个读数都留着——「预算放大之后跑不跑得完」本身就是一条读数。


def missing_diagnostic_ids(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """诊断臂里「没有块」的题号（按题集）：30 s 内没跑完的那些。"""

    plan: dict[str, list[str]] = {}
    for name, batch in payload["in_batches"].items():
        plan[name] = [
            row["id"]
            for row in batch["rows"]
            if row[ARM_DIAGNOSTIC].get("orchestration_llm_calls") is None
        ]
    return plan


def merge_diagnostic_arm(
    payload: dict[str, Any],
    *,
    batch_name: str,
    outcomes: Sequence[Any],
    timeout_ms: int,
) -> list[str]:
    """把一次「诊断臂重跑」的读数并回产物：**只**替换诊断臂那一列，另外两臂一个字不动。

    每一行都记着自己这一档预算是多少（``on_diagnostic_budget_ms``），并且把编排块
    （要点清单 + 核心片**正文**）留档——抽检要在产物上做，不是再跑一次实验。
    """

    batch = payload["in_batches"][batch_name]
    replaced: list[str] = []
    for row in batch["rows"]:
        outcome = next((item for item in outcomes if item.id == row["id"]), None)
        if outcome is None:
            continue
        row[ARM_DIAGNOSTIC] = material_facts(outcome, orchestration_enabled=True)
        row[f"{ARM_DIAGNOSTIC}_latency_ms"] = outcome.latency_ms
        row[f"{ARM_DIAGNOSTIC}_model_calls"] = outcome.model_calls
        row[f"{ARM_DIAGNOSTIC}_degraded_reason"] = outcome.degraded_reason
        row["on_diagnostic_budget_ms"] = timeout_ms
        row["on_diagnostic_block"] = (
            audit_block(outcome.orchestration) if outcome.orchestration else None
        )
        replaced.append(row["id"])
    batch["aggregate"] = aggregate(batch["rows"])
    return replaced


async def _refill_diagnostic(
    payload: dict[str, Any],
    *,
    timeout_ms: int,
    only_missing: bool,
) -> dict[str, Any]:
    """按 ``timeout_ms`` 预算重跑诊断臂（默认全部题），并把读数 + 编排块正文并回产物。

    为什么要有这一步（不是「跑两次挑好看的」）：部署臂在 4 s 预算下 100% 降级，
    诊断臂 30 s 也只跑完一部分——**材料量与完整度的读数**只有在编排真的跑完时才有意义。
    所以把整步预算放大到 60 s 再跑一遍，并把「哪一档预算跑出什么」逐行记进产物。
    """

    from scripts import eval_refs, eval_retrieval

    payload["llm_chain"] = await eval_refs._bind_model_chain()
    for name, batch in payload["in_batches"].items():
        rows = batch["rows"]
        targets = (
            [
                row["id"]
                for row in rows
                if row[ARM_DIAGNOSTIC].get("orchestration_llm_calls") is None
            ]
            if only_missing
            else [row["id"] for row in rows]
        )
        if not targets:
            continue
        kb_ids = list(batch["kb_ids"])
        tenant_id = str(batch["tenant_id"])
        _apply_batch_settings(
            keyword_alias=str(batch["keyword_alias"]),
            rerank=False,
            orchestration_timeout_ms=timeout_ms,
        )
        warmup = await _warm_up(
            kb_ids=kb_ids,
            tenant_id=tenant_id,
            label=f"{name}:diagnostic-refill",
            orchestration_timeout_ms=timeout_ms,
        )
        questions = [
            {"id": row["id"], "query": row["query"], "mechanism": row["mechanism"]}
            for row in rows
            if row["id"] in set(targets)
        ]
        print(f"诊断臂重跑 {name}：{len(questions)} 题（整步预算 {timeout_ms} ms）")
        outcomes = await _run_arm(questions, kb_ids=kb_ids, tenant_id=tenant_id, orchestration=True)
        replaced = merge_diagnostic_arm(
            payload, batch_name=name, outcomes=outcomes, timeout_ms=timeout_ms
        )
        # 旁证（引用语料的期望节覆盖 / 137 题的 CP-CR）跟着重跑读数一起更新。
        if name == "refs":
            source = eval_refs.load_questions()["questions"]
            coverage = eval_refs.coverage_by_question(outcomes, source)
            for row in rows:
                if row["id"] in set(replaced) and row["id"] in coverage:
                    row["coverage_on_diagnostic"] = coverage[row["id"]]
        else:
            gate_selected = select_gate_questions(eval_retrieval._load_questions()[0])
            selected = [item for item in gate_selected if item.id in set(replaced)]
            if selected:
                scores = await _gate_scores(questions=selected, outcomes=outcomes)
                for row in rows:
                    if row["id"] in scores:
                        row["cp_on_diagnostic"] = scores[row["id"]]["context_precision"]
                        row["cr_on_diagnostic"] = scores[row["id"]]["context_recall"]
        batch["diagnostic_refill"] = {
            "timeout_ms": timeout_ms,
            "ids": replaced,
            "warmup": warmup,
            "still_degraded": [
                row["id"]
                for row in rows
                if row["id"] in set(replaced)
                and row[ARM_DIAGNOSTIC].get("orchestration_llm_calls") is None
            ],
        }
    payload["refilled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def _refill_main(path: Path, *, timeout_ms: int, only_missing: bool) -> int:
    """读一份已有产物，按 ``timeout_ms`` 预算重跑诊断臂，把读数与编排块正文并回去。"""

    from app.core.logging import setup_logging

    payload = json.loads(path.read_text(encoding="utf-8"))
    setup_logging()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    payload = asyncio.run(
        _refill_diagnostic(payload, timeout_ms=timeout_ms, only_missing=only_missing)
    )
    # 先落盘再记账：这一段是长跑之后的收尾，任何一步失败都不该把已经跑出来的读数丢掉
    # （第一次真机上就是在记账时抛错，把重跑的读数一起丢掉的）。
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload["accounted_calls"] = _accounted_calls(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"产物（已并回）：{path}")
    for name, batch in payload["in_batches"].items():
        refill = batch.get("diagnostic_refill") or {}
        if refill:
            print(
                f"  {name}: 重跑 {len(refill['ids'])} 题（{refill['timeout_ms']} ms）；"
                f"仍缺块 {refill['still_degraded'] or '无'}"
            )
    accounted = payload["accounted_calls"]
    print(f"记账调用数：{accounted['total_lower_bound']}~{accounted['total_upper_bound']} 次")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "T159 group 1: run the same questions with orchestration off, on (deployed "
            "budget) and on (diagnostic budget), recording material volume / points / "
            "completeness / calls / latency."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="只选题 + 打印预估，不连库")
    parser.add_argument(
        "--rerank", choices=("off", "on"), default="off", help="重排开关（默认关，与基线同口径）"
    )
    parser.add_argument("--out-dir", default=None, help="产物目录（缺省 eval/reports/t159/run-*）")
    parser.add_argument(
        "--refill-results",
        default=None,
        help="给一份 results.json：按 --diagnostic-timeout-ms 重跑诊断臂并写回（不重跑别的臂）",
    )
    parser.add_argument(
        "--diagnostic-timeout-ms",
        type=int,
        default=RETRY_TIMEOUT_MS,
        help="重跑诊断臂用的整步预算（缺省 60 s）",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="只重跑诊断臂里缺块的题（缺省 = 整臂重跑，便于把正文一起收进产物）",
    )
    args = parser.parse_args(argv)
    if args.refill_results:
        return _refill_main(
            Path(args.refill_results),
            timeout_ms=args.diagnostic_timeout_ms,
            only_missing=args.only_missing,
        )

    refs_count, gate_count = _selection_count()
    predicted = predicted_calls(
        refs_questions=refs_count,
        gate_questions=gate_count,
        rerank=args.rerank == "on",
    )
    print(
        f"预计模型调用 ≤ {predicted['total_upper']} 次（上界：暖机 ×2 题集 + 三臂检索"
        f" {predicted['off_calls']}×3 + 编排 ≤{predicted['orchestration_upper']}；"
        f"重排 {predicted['assumptions']['rerank']}）"
    )
    print(f"题集：refs {refs_count} 道 + gate {gate_count} 道（{GATE_TYPE}）")
    if args.dry_run:
        return 0

    from app.core.logging import setup_logging

    setup_logging()
    if sys.platform == "win32":
        # psycopg 异步连接在 Windows 的缺省 Proactor 循环上跑不起来（既有评测脚本同一处理）。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    payload = asyncio.run(_run_batches(rerank=args.rerank == "on"))

    actual = _accounted_calls(payload)
    payload["predicted"] = predicted
    payload["accounted_calls"] = actual
    target = write_results(payload, out_dir=Path(args.out_dir) if args.out_dir else None)
    print(f"产物：{target}")
    for name, batch in payload["in_batches"].items():
        summary = batch["aggregate"]
        print(f"  {name}: {summary['questions']} 题")
        for arm in ARMS:
            arm_summary = summary["arms"][arm]
            print(
                f"    {arm}: 材料 {arm_summary['materials_total']} 条/"
                f"{arm_summary['materials_chars_total']} 字；"
                f"挂上要点 {arm_summary['assigned_materials_total']}；"
                f"完整度 {arm_summary['completeness']}；"
                f"降级 {arm_summary['degraded']['count']}；"
                f"p50 {arm_summary['latency']['p50']}ms"
            )
    print(
        f"记账调用数：{actual['total_lower_bound']}~{actual['total_upper_bound']} 次"
        f"（预估上界 {predicted['total_upper']}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
