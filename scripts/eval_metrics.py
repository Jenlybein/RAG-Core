"""评测打分核心：**纯函数、零依赖、可离线单测**。

为什么把它从 ``scripts/eval_retrieval.py`` 里拆出来：脚本本身要连真 PostgreSQL、
真 Elasticsearch、真嵌入，跑一次有网络与容器成本；而「分数怎么算」是纯算术。
两者混在一起，算分逻辑就只能靠「跑一遍真链路看数字像不像」来验证——那既慢又不
可证伪。这里只认 ``Case``（题号、题型、相关切片、实际返回顺序），不碰数据库、不碰
HTTP，于是可以用手算真值表把每个指标钉死。

三个主指标的口径（v1 的教训写在下面）：
``P@k`` 的分母是**实际返回条数**，``R@k`` 的分母是**该题全部相关切片数**，
``MRR`` 只看第一条相关切片排第几。另有两个只对特定题型有意义的量：

* ``hit_at_1``：第一条返回是不是相关切片——``Recall@5`` 到顶时它是唯一还分得出的格子；
* ``exact_hit_at_1``：第一条返回**是不是被问的那个编号切片**。问 E1025 而把 E1024
  排第一，算不中。v1 只有「命中了某个错误码」这一层，所以两种模式同时 1.0000。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# 编号类题型：只有这些题型的「第一条是不是被问的那一片」值得单独报。
IDENTIFIER_TYPES = frozenset({"exact_code", "rare_token"})

_TOL = 1e-12


@dataclass(frozen=True)
class Case:
    """一道题在一种 mode 下的一次真实返回，已经剥离成算分需要的最小信息。"""

    id: str
    type: str
    relevant: frozenset[str]
    returned: tuple[str, ...]
    # 陷阱对里「不该被排第一、但语义上几乎一样」的那一片（向量分不开的邻居）。
    trap: frozenset[str] = field(default_factory=frozenset)


# ─────────────────────────────────────────────────────────────── 单题基础量


def _prefix(returned: Sequence[str], k: int) -> tuple[str, ...]:
    return tuple(returned[:k])


def precision_at(returned: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """``P@k`` = 前 k 条里的命中数 / 前 k 条的**实际条数**。

    分母用实际条数：返回 2 条时按 2 条算，否则「少返回」会被算成「不精确」，
    把一个降级 / 空结果问题伪装成排序问题。
    """

    if not relevant:
        return 0.0
    prefix = _prefix(returned, k)
    if not prefix:
        return 0.0
    return sum(1 for chunk_id in prefix if chunk_id in relevant) / len(prefix)


def recall_at(returned: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """``R@k`` = 前 k 条里找到的**不同**相关切片数 / 该题相关切片总数。

    用集合去重：同一片被两路召回成两行时，召回率说的是「找到几成」而不是「占几行」。
    """

    if not relevant:
        return 0.0
    found = {chunk_id for chunk_id in _prefix(returned, k) if chunk_id in relevant}
    return len(found) / len(relevant)


def first_relevant_rank(returned: Sequence[str], relevant: frozenset[str]) -> int | None:
    """第一条相关切片的名次（1 起）；一条都没有就是 ``None``。"""

    for position, chunk_id in enumerate(returned, start=1):
        if chunk_id in relevant:
            return position
    return None


def reciprocal_rank(returned: Sequence[str], relevant: frozenset[str]) -> float:
    rank = first_relevant_rank(returned, relevant)
    return 0.0 if rank is None else 1.0 / rank


def hit_at_1(returned: Sequence[str], relevant: frozenset[str]) -> float:
    """第一条返回是不是相关切片。多标签题（跨章节）同样成立：第一片属于相关集合即可。"""

    if not relevant or not returned:
        return 0.0
    return 1.0 if returned[0] in relevant else 0.0


# ───────────────────────────────────────────────────────────────── 汇总


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def aggregate(cases: Sequence[Case], k: int = 5) -> dict[str, object]:
    """一组题的宏平均。``absent``（相关为空）题**不进主平均**。

    反向对照题测的是「该不该返回」；把它的 0/1 混进召回均值，会让「诚实空」这一个
    行为左右整个分数。它单独由 :func:`absent_report` 报。
    """

    answerable = [case for case in cases if case.relevant]
    if not answerable:
        return {
            "count": 0,
            "precision_at_k": None,
            "recall_at_k": None,
            "mrr": None,
            "recall_at_1": None,
            "hit_at_1": None,
        }
    return {
        "count": len(answerable),
        "precision_at_k": _mean(
            precision_at(case.returned, case.relevant, k) for case in answerable
        ),
        "recall_at_k": _mean(recall_at(case.returned, case.relevant, k) for case in answerable),
        "mrr": _mean(reciprocal_rank(case.returned, case.relevant) for case in answerable),
        "recall_at_1": _mean(recall_at(case.returned, case.relevant, 1) for case in answerable),
        "hit_at_1": _mean(hit_at_1(case.returned, case.relevant) for case in answerable),
    }


def by_type(cases: Sequence[Case], k: int = 5) -> dict[str, dict[str, object]]:
    """按题型拆开算同一组指标：总平均会掩盖「某一类题只有关键词能救」。"""

    grouped: dict[str, list[Case]] = {}
    for case in cases:
        grouped.setdefault(case.type, []).append(case)
    return {type_name: aggregate(grouped[type_name], k=k) for type_name in sorted(grouped)}


def exact_hit_at_1(
    cases: Sequence[Case], types: Iterable[str] = IDENTIFIER_TYPES
) -> dict[str, object]:
    """编号精确性：第一条返回**就是被问的那一片**的比例。

    只统计编号类题型。问 ``E1025`` 却把 ``E1024`` 排第一算不中——这是「命中某个错误码」
    与「命中被问的那个错误码」之间的差距，也是本次重做要能分辨的东西。
    """

    wanted = set(types)
    subset = [case for case in cases if case.type in wanted and case.relevant]
    hits = sum(1 for case in subset if hit_at_1(case.returned, case.relevant))
    return {
        "count": len(subset),
        "hits": hits,
        "rate": (hits / len(subset)) if subset else None,
    }


# ─────────────────────────────────────────────────────── 陷阱对与误召报告


def trap_report(cases: Sequence[Case]) -> list[dict[str, object]]:
    """陷阱对实测：被问的那一片排第几、它的孪生兄弟有没有被一起召回、排第几。

    这三件事必须分开报：``correct_at_1``（问对了没有）、``twin_rank``（错误的那个
    排到哪）与 ``twin_returned``（它到底进没进前 k）。v1 只报第一件，所以看不出
    「两片同分、名次随机」这种失败方式。
    """

    report: list[dict[str, object]] = []
    for case in cases:
        if not case.trap:
            continue
        twin_rank = first_relevant_rank(case.returned, case.trap)
        report.append(
            {
                "id": case.id,
                "type": case.type,
                "trap_chunk_ids": sorted(case.trap),
                "correct_at_1": bool(hit_at_1(case.returned, case.relevant)),
                "twin_rank": twin_rank,
                "twin_returned": twin_rank is not None,
            }
        )
    return report


def absent_report(cases: Sequence[Case]) -> dict[str, object]:
    """反向对照题（库里确实没有）：只报「返回了几条 / 误召了吗」。

    绝不把它们的精确率算成 1 然后混进主平均——那会让「返回空」这一种行为凭空赚分。
    """

    subset = [case for case in cases if not case.relevant]
    if not subset:
        return {
            "count": 0,
            "returned_total": 0,
            "returned_avg": None,
            "false_positive_rate": None,
            "per_question": [],
        }
    returned_total = sum(len(case.returned) for case in subset)
    flagged = sum(1 for case in subset if case.returned)
    return {
        "count": len(subset),
        "returned_total": returned_total,
        "returned_avg": returned_total / len(subset),
        "false_positive_rate": flagged / len(subset),
        "per_question": [
            {
                "id": case.id,
                "returned_total": len(case.returned),
                "returned": list(case.returned),
            }
            for case in subset
        ],
    }


# ─────────────────────────────────────────────────────────────── 题型赢家


def _rank_key(score: Mapping[str, object] | None) -> tuple[float, float, float]:
    if not score:
        return (-1.0, -1.0, -1.0)
    return (
        float(score.get("recall_at_k") or 0.0),
        float(score.get("mrr") or 0.0),
        float(score.get("precision_at_k") or 0.0),
    )


def winner(scores: Mapping[str, Mapping[str, object] | None]) -> str:
    """按「召回 → MRR → 精确率」的字典序选出赢家；三项都相等才算打平。

    为什么召回优先：本票要回答的是「混合有没有把关键词能捞到、向量捞不到的补回来」。
    召回打平时才轮到名次（MRR）说明「同样的切片被提前了」。
    """

    ordered = sorted(scores.items(), key=lambda item: _rank_key(item[1]), reverse=True)
    if not ordered:
        return "打平"
    top_key = _rank_key(ordered[0][1])
    tied = [
        name
        for name, score in ordered
        if all(abs(left - right) <= _TOL for left, right in zip(top_key, _rank_key(score)))
    ]
    # 并列第一就是打平：只因为排在后面的第三名更差，不能反过来说第一名唯一。
    return tied[0] if len(tied) == 1 else "打平"
