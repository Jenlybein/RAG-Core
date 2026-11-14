"""编排整步预算的默认值守卫（#159 实测之后定下来的）。

**为什么要有这条**：`ORCHESTRATION_TIMEOUT_MS` 最初写的 4000 让这个能力在生产默认配置下
**20/20 全部降级、一块都产不出**，而所有用例都是绿的——因为用例自己给了 `timeout_ms`。
这类「交付即死」只能靠端到端跑批发现，发现之后必须留一个绊索，否则下次调参又调回去。
"""

from __future__ import annotations

from app.core.config import Settings


def test_the_default_budget_covers_the_measured_step_latency() -> None:
    """默认整步预算必须覆盖实测的步级延迟，不能覆盖就要在这里说清楚为什么。

    实测（``eval/reports/t159/t159-orchestration-on-off.md``）：

    * 单次结构化调用 4.5–25 秒；
    * 编排是**两次串行**，共用这一份预算；
    * 步级 p50 **22.5 秒**，30 秒档 13/20 跑完，60 秒档 20/20 跑完。

    所以默认值低于 22.5 秒就等于「这个能力永远出不了块」。
    """

    assert Settings.model_fields["ORCHESTRATION_TIMEOUT_MS"].default >= 22_500, (
        "默认值低于实测 p50（22.5 秒）：两次串行调用共用一个预算，低了就是 100% 降级"
    )


def test_the_default_is_not_so_large_that_a_hung_provider_stalls_the_request() -> None:
    """上限也不能无限大：这是个同步能力，卡死时整条检索跟着卡。

    60 秒是这个能力的实测上界（20/20 跑完那档），超过它不是一个「预算」而是一次事故。
    """

    assert Settings.model_fields["ORCHESTRATION_TIMEOUT_MS"].default <= 60_000
