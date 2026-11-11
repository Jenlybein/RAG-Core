"""低分样本的逻辑内核（#110）：筛选、对齐、合并、产物形状。

**原文永远只在参数里、只在内存里过一遍**：本模块不写业务库、不写日志、不写 JSONL。
问句原文由调用方从观测服务取来（`query_of` 注入），这里只把它拼进一个「候选评测题」的
字典——`原文不落库`（ADR-0021 决策 1）是整条链路的红线。

三件事值得单独说清：

1. **取不到原文就是失败，不是空结果。** 观测服务不可用 / 没配 / 这条 trace 不在栈里，
   ``query_of`` 抛异常即向外抛；``build_candidates`` 一条都不产出。悄悄导出一份缺 question
   的候选集，比明确报错更坏——它会让人以为「这轮没有差评」。
2. **产物形状与题库同源，但 `relevant` 故意为空。** 差评变成评测题之后，还需要人工标
   「哪几片真正相关」；空标注是「待办」，不是「库里没有答案」。所以类型标成 ``candidate``，
   由 ``eval_gate.validate_dataset`` 之外的一条更宽松的自检来验形状（那个校验器要求
   非 absent 题必须有标注，候选集天生不满足，硬套等于永远过不了）。
3. **增量合并按规范化问句去重**：同一句口语化问法被三个人各差评一次，只该进题库一次。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol

#: 候选题的固定题型。它不是题库里的 12 类之一，标出来是为了让人一眼看出「还没标」。
CANDIDATE_TYPE = "candidate"


class QuerySource(Protocol):
    """观测服务的取原文接口：给 trace_id，回问句原文；取不到就抛。"""

    def query_of(self, trace_id: str) -> str: ...


@dataclass(frozen=True)
class LowScoreSample:
    """一条本地筛选出来的差评事实（**不含原文**）。"""

    log_id: str
    trace_id: str
    tenant_id: str
    kb_ids: tuple[str, ...]
    score: int
    created_at: str


@dataclass(frozen=True)
class Candidate:
    """一条候选评测题（含原文，只在内存里）。"""

    id: str
    query: str
    trace_id: str
    log_id: str
    kb_ids: tuple[str, ...]
    score: int


def normalize_query(query: str) -> str:
    """规范化问句：**去掉全部空白**（含全角空格、换行、词间空格）。

    中文问法里空格几乎都是打字噪声：「退款多久到账」与「退款 多久到账」是同一句差评，
    不去重会让同一道题在题库里被记两遍。英文/数字之间也不保留空格——候选集只按
    「逐字相同」判重，宁可少去重也不把两句不同的问题并成一句。

    小写化不做：中文没有大小写，而错误码 / 型号（``E1024`` / ``XR-2000``）的大小写
    本身就是区分信息。
    """

    return "".join(query.split()).replace("\u3000", "")


def select_low_scores(
    samples: Iterable[LowScoreSample],
    *,
    max_score: int,
    days: int,
    now_epoch: float,
    kb_filter: Sequence[str] | None = None,
) -> list[LowScoreSample]:
    """按分数阈值 / 时间窗 / 知识库过滤；纯函数，好离线测。

    ``created_at`` 用 ISO 字符串传入（仓储层从 datetime 转），这里只比一个 epoch 秒数，
    不引入时区库——时间窗是「最近 N 天」的粗粒度筛选，精确到秒没有意义。
    """

    from datetime import datetime, timezone

    cutoff = now_epoch - days * 86400
    wanted = set(kb_filter or ())
    out: list[LowScoreSample] = []
    for sample in samples:
        if sample.score > max_score:
            continue
        if wanted and not (set(sample.kb_ids) & wanted):
            continue
        try:
            stamp = datetime.fromisoformat(sample.created_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            when = stamp.timestamp()
        except ValueError:
            continue
        if when < cutoff:
            continue
        out.append(sample)
    return out


def build_candidates(
    samples: Sequence[LowScoreSample],
    *,
    query_of: Callable[[str], str],
) -> list[Candidate]:
    """本地差评 + 观测服务原文 → 候选题。**任何一条取不到原文都整轮失败**。"""

    out: list[Candidate] = []
    for sample in samples:
        query = query_of(sample.trace_id).strip()
        if not query:
            raise RuntimeError(f"trace {sample.trace_id}: 观测服务返回了空的问句原文")
        out.append(
            Candidate(
                id=f"fb-{sample.log_id}",
                query=query,
                trace_id=sample.trace_id,
                log_id=sample.log_id,
                kb_ids=sample.kb_ids,
                score=sample.score,
            )
        )
    return out


def merge_candidates(
    existing: Sequence[Mapping[str, Any]], new: Sequence[Candidate]
) -> tuple[list[dict[str, Any]], list[Candidate]]:
    """按规范化问句增量合并；返回（合并后全部题，实际新增的题）。

    已有条目只要有同样的规范化问句就跳过——不比 `id`，因为 id 里带 log_id，
    同一句问句的两次差评天然是两个 id。
    """

    rows: list[dict[str, Any]] = [dict(item) for item in existing]
    seen = {normalize_query(str(item.get("query") or "")) for item in existing}
    added: list[Candidate] = []
    for candidate in new:
        key = normalize_query(candidate.query)
        if key in seen:
            continue
        seen.add(key)
        added.append(candidate)
        rows.append(as_case(candidate))
    return rows, added


def as_case(candidate: Candidate) -> dict[str, Any]:
    """一条候选题的题库形状。**`relevant: []` 是待标注，不是「库里没有答案」。**"""

    return {
        "id": candidate.id,
        "type": CANDIDATE_TYPE,
        "query": candidate.query,
        "relevant": [],
        "note": "来自低分反馈，待人工标注相关切片",
        "source": {
            "trace_id": candidate.trace_id,
            "log_id": candidate.log_id,
            "kb_ids": list(candidate.kb_ids),
            "score": candidate.score,
        },
    }


def validate_candidates(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """候选集的形状自检（比题库校验宽松，只查「能被人工接手」的最低条件）。"""

    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    for index, row in enumerate(rows):
        qid = str(row.get("id") or "")
        query = str(row.get("query") or "")
        if not qid:
            problems.append(f"第 {index} 条缺 id")
        elif qid in seen_ids:
            problems.append(f"{qid}: id 重复")
        seen_ids.add(qid)
        if not query.strip():
            problems.append(f"{qid}: query 为空")
        key = normalize_query(query)
        if key and key in seen_queries:
            problems.append(f"{qid}: 与已有题同一句问句（合并没去重）")
        seen_queries.add(key)
        if row.get("relevant") not in ([], None) or not isinstance(row.get("relevant"), list):
            problems.append(f"{qid}: 候选题的 relevant 必须是空列表（待人工标注）")
    return problems


__all__ = [
    "CANDIDATE_TYPE",
    "Candidate",
    "LowScoreSample",
    "QuerySource",
    "as_case",
    "build_candidates",
    "merge_candidates",
    "normalize_query",
    "select_low_scores",
    "validate_candidates",
]
