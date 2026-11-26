"""#214 · 构建收敛的**判据**：真值够不够、有没有失败明细，四个组合作出一个结论。

这张票要堵的是「假计数 → 空版本上线」：旧实现丢掉每篇重建函数的返回值、抢不到也计一篇
「已重建」，而唯一的「一篇都没重建」守卫数的正是那个假计数——四项计数在 0=0=0 时三方相等，
于是空版本一路 ready 并激活，线上静默变成「版本正常、检索全空」
（[ADR-0034](../../docs/adr/0034-queue-state-and-build-convergence.md) 决策四）。

判据因此被抽成一个**纯函数**（:func:`app.services.index_build_service.decide_convergence`）：
输入三个数字（真值 / 参与篇数 / 失败明细）加一个「是不是最后一次」，输出一个结论。于是
「四种组合各自判成什么」在这里逐条钉死，不需要连库、造切片、摆时间——那些属于
``tests/test_integration_build_convergence.py``（真库），这一份只管判据本身。

还有一条同样纯的：**收敛期限**的公式。它决定「等多久算够了」，错的方向有两个——判早了把
正常构建打成失败，判晚了让一个死掉的构建永远转圈。所以它的形状（与篇数相关、封顶、
不从「跑到第几篇」推）也在这里钉住。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.index_version import (
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
)
from app.services.index_build_service import (
    CONVERGE_BASE_SECONDS,
    CONVERGE_MAX_SECONDS,
    CONVERGE_PER_DOCUMENT_SECONDS,
    CONVERGE_WAIT,
    BuildTruth,
    converge_deadline_for,
    decide_convergence,
)


# ────────────────────────────────────── 四种组合：真值够/不够 × 有失败/无失败


def test_enough_truth_without_failures_is_ready() -> None:
    """该建的都建了、也没有失败明细 → ``ready``（这是唯一通向 ready 的那一格）。"""

    assert decide_convergence(truth=5, planned=5, failed=0) == INDEX_BUILD_READY
    assert decide_convergence(truth=7, planned=5, failed=0) == INDEX_BUILD_READY, (
        "真值超过快照（构建期间又进来了文档）也算够：欠的是「至少盖满」，不是「恰好相等」"
    )


def test_enough_truth_with_failures_is_still_failed() -> None:
    """真值够但有失败明细 → ``failed``：部分版本**永不上线**（ADR-0025）。

    这一格最容易做错：看起来「篇数够了」，于是顺手 ready。可有失败明细说明有一批文档
    卡住了，这一次的结论必须是「这次没成」——让运维带着明细去查，而不是让它悄悄上线。
    """

    assert decide_convergence(truth=5, planned=5, failed=1) == INDEX_BUILD_FAILED
    assert decide_convergence(truth=9, planned=5, failed=3) == INDEX_BUILD_FAILED


def test_not_enough_truth_without_failures_is_failed_on_the_last_chance() -> None:
    """真值不够、也没人报失败，且这是最后一次机会 → ``failed``。

    「不够」有两种读法：「还差几篇，可能还在飞」与「不会有人再补了」。任务跑完整个库时
    循环结束时就是后者（``final=True``，这一票的形状）；按篇投递之后由收敛期限来宣布它。
    """

    assert decide_convergence(truth=3, planned=5, failed=0, final=True) == INDEX_BUILD_FAILED


def test_not_enough_truth_before_the_deadline_only_waits() -> None:
    """同样的读数，若还有人在飞（``final=False``）→ ``wait``，不迁移任何状态。

    这一格是「按篇投递」与清扫器要的那一格：某一篇刚跑完时它看见的真值必然还差几篇，
    此时判死会把一次正常的构建打回去。等多久由构建行的收敛期限说了算。
    """

    assert decide_convergence(truth=3, planned=5, failed=0, final=False) == CONVERGE_WAIT


def test_not_enough_truth_with_failures_fails_even_before_the_deadline() -> None:
    """有失败明细时**不等**：等下去也不会让那几篇变成成功。"""

    assert decide_convergence(truth=3, planned=5, failed=2, final=False) == INDEX_BUILD_FAILED


# ────────────────────────────────────── 两条边界：空库 / 一篇都没进来


def test_an_empty_library_builds_an_empty_version() -> None:
    """起构建时这个库就没有可参与的文档（0 / 0）→ ``ready``。

    空版本在**这个**前提上是诚实的结果（老实现的同一口径）。要挡住的是另一件事：
    库里有文档、却一篇都没进来（下一格）。
    """

    assert decide_convergence(truth=0, planned=0, failed=0) == INDEX_BUILD_READY


def test_a_build_that_wrote_nothing_must_fail_not_ready() -> None:
    """库里有文档、真值为 0 → ``failed``，**绝不是 ready**。

    这就是空版本上线的那条路：``truth == 0`` 而 ``planned > 0``。旧实现里这一格能一路
    ready 并激活——四项计数在「切片 0 / 向量 0 / 关键词 0」时是自洽的，闸门拦不住它。
    """

    assert decide_convergence(truth=0, planned=3, failed=0) == INDEX_BUILD_FAILED
    assert decide_convergence(truth=0, planned=1, failed=0) == INDEX_BUILD_FAILED


def test_a_negative_truth_is_treated_as_a_shortfall() -> None:
    """真值不可能是负数；真出现了也只是「不够」，不该因为一个坏读数变成 ready。"""

    assert decide_convergence(truth=-1, planned=2, failed=0) == INDEX_BUILD_FAILED


# ────────────────────────────────────── 真值读数快照


def test_the_shortfall_is_never_negative_and_says_how_many_are_missing() -> None:
    """「还缺几篇」= 参与篇数 − 真值；真值超过快照时回 0（那是文档变多了，不是欠账）。"""

    assert BuildTruth(documents=3, planned=5).shortfall == 2
    assert BuildTruth(documents=5, planned=5).shortfall == 0
    assert BuildTruth(documents=8, planned=5).shortfall == 0


def test_the_truth_snapshot_flattens_into_loggable_fields() -> None:
    """读数要能整份进日志：四个字段一个都不少（缺一个就得回头查库才知道当时读到什么）。"""

    assert BuildTruth(documents=3, planned=5, doc_version=2).as_dict() == {
        "documents": 3,
        "planned": 5,
        "doc_version": 2,
        "shortfall": 2,
    }


# ────────────────────────────────────── 收敛期限的公式


def test_the_deadline_grows_with_the_library_but_is_capped() -> None:
    """期限与库规模相关（篇数越多越晚），且有封顶。"""

    started = datetime(2026, 11, 26, 12, 0, 0)

    small = converge_deadline_for(0, started_at=started)
    assert small == started + timedelta(seconds=CONVERGE_BASE_SECONDS), (
        "一篇都没有时也要留出「投递 → 起任务」的余量，所以有个基数"
    )

    one = converge_deadline_for(1, started_at=started)
    big = converge_deadline_for(500, started_at=started)
    assert one < big, "篇数越多期限越晚——期限是「整库跑完」的预算，不是单篇的"
    assert (big - one) == timedelta(seconds=CONVERGE_PER_DOCUMENT_SECONDS * 499)

    huge = converge_deadline_for(10**6, started_at=started)
    assert huge == started + timedelta(seconds=CONVERGE_MAX_SECONDS), "再大的库也有封顶"


def test_the_deadline_is_a_constant_offset_not_a_progress_reading() -> None:
    """同一个篇数、同一个开始时刻 → 同一个期限（不掺「现在跑到第几篇」这类活数据）。"""

    started = datetime(2026, 11, 26, 12, 0, 0)
    assert converge_deadline_for(3, started_at=started) == converge_deadline_for(
        3, started_at=started
    )


def test_a_negative_planned_count_does_not_shorten_the_base_budget() -> None:
    """坏输入（负数）按 0 算：兜底不该因为一个读错的库变成一个立刻过期的死线。"""

    started = datetime(2026, 11, 26, 12, 0, 0)
    assert converge_deadline_for(-5, started_at=started) == converge_deadline_for(
        0, started_at=started
    )
