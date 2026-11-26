"""#222 · 每日切块额度：``plans.chunk_daily_tokens`` + 两处记账列

## 为什么需要第四本账

三本账（检索 / 重排 / 大模型）数的都是**请求侧的调用次数**。它们漏掉了一整类成本：
**索引期**的 embedding 消耗。一次整库重建把 5000 篇文档重新向量化，账上一点痕迹都没有；
恶意或误操作的重建可以把嵌入余额一天烧光，而所有面板显示「今天没怎么用」。

ADR-0035 决策三把这条定为第四本账：**按 token 计、只有租户级**（Worker 里没有「哪把 Key
发起的」这个概念——索引是后台动作，不是某一次 API 调用）。

## 两列各干什么

| 列 | 表 | 作用 |
| --- | --- | --- |
| ``meter_chunk`` | ``usage_events`` | 明细：一次索引消耗多少检索文本 token |
| ``meter_chunk`` | ``usage_daily`` | 汇总：某天某租户的切块总量 |

``usage_events`` 那一列必须配一个**专门的端点标记**（``endpoint = 'index'``），因为用量页的
「调用次数 / 成功率 / P50 / P95」全都是按请求行算的：索引行混进去会让成功率莫名其妙地掉
（索引没有「成功/失败」的一半口径），也会让 P95 被一个几分钟的批次拉爆。排除规则落在
``usage_stats`` / ``usage_rollup`` 里，并用用例钉住——**这条规则是本次最容易忘的一条**。

## 为什么额度是 token 而不是片数

片数（``len(chunks)``）直观，但它对成本的解释力弱：同样 1000 片，每片 200 token 与每片
800 token 差四倍。而账单按 token 结——用片数做额度，等于用一个与账单不成比例的尺子管钱。

额度记的是**检索文本 token**（真正发给嵌入模型的那一份），不是正文 token：两者差 10–20%
的标题与节说明前缀，而账单认的是前者。

## downgrade 是完整的

三列都是本次新增的派生量（当日额度可以从明细重算），删掉即回到上一版。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6a1d8c5f247"
down_revision: str | None = "d5f9c2a7b3e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 四档种子值（ADR-0035 决策六）。第二份拷贝的理由见 ``d5f9c2a7b3e8`` 的同名注释。
_TIER_VALUES: dict[str, dict[str, int]] = {
    "free": {"chunk_daily_tokens": 300_000},
    "plus": {"chunk_daily_tokens": 4_000_000},
    "pro": {"chunk_daily_tokens": 9_000_000},
    "max": {"chunk_daily_tokens": 15_000_000},
}


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "chunk_daily_tokens", sa.Integer(), nullable=False, server_default="300000"
        ),
    )
    for code, values in _TIER_VALUES.items():
        op.execute(
            sa.text(
                "UPDATE plans SET chunk_daily_tokens = :limit WHERE code = :code"
            ).bindparams(limit=values["chunk_daily_tokens"], code=code)
        )

    # 明细与汇总各加一列。默认 0 = 「这次没有切块消耗」，与 meter_r/rr/llm 同一口径。
    op.add_column(
        "usage_events",
        sa.Column("meter_chunk", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "usage_daily",
        sa.Column("meter_chunk", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("usage_daily", "meter_chunk")
    op.drop_column("usage_events", "meter_chunk")
    op.drop_column("plans", "chunk_daily_tokens")
