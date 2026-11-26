"""表：``usage_daily`` —— 按天汇总（第 14 步新增，第一次让用量能回溯 13 个月）。

## 为什么必须有它

``usage_events`` 是一行一次请求，量级按「每租户每天几千行」长；而看板要回答的是
**「过去 30 天怎么走」「上个月这个租户多少」**。直接扫明细有两个问题：90 天前的行已经被
保留策略删了，以及 30 天的 group by 会越来越慢。所以按天压一次：一行代表
「某天 · 某租户 · 某 Key · 某端点 · 某业务码」这一格。

## 为什么存直方图而不是存一个 P95

**分位数不能相加。** 「每天一个 P95」再加平均，算出来的是「每天 P95 的中位数」，不是这 30 天
的 P95——它看起来像个分位数，但不是，而且没有任何迹象会告诉你它错。所以这里存
**7 档延迟计数**（``lat_b0`` … ``lat_b6``），查询时按桶插值算分位，误差约 13%，
响应里带 ``p95_source = "histogram"`` 把这件事说出来。

## 为什么维度的空值用空串而不是 NULL

``(day, tenant_id, key_id, endpoint, code)`` 是**主键**，而 Postgres 的主键列不允许 NULL。
而这些维度都天然会缺：老行没有 ``key_id``（第 14 步之前没记），也没有 ``endpoint`` 语义。
所以约定 **``""`` = 未知/历史汇总**，而不是 NULL：主键成立、``ON CONFLICT`` 的幂等 upsert 也成立
（``NULL != NULL`` 会让唯一约束形同虚设，同一格会被重复插入）。

``code`` 同理用整数：``0`` 是成功、其余是业务码；**迁移之前的历史行用 ``-1``**（``UNKNOWN_CODE``），
不是 0。它们没有码可记，算成成功会让成功率凭空变好、算成失败会凭空变坏——所以读侧把它们
单独列成「未统计」，并且不进成功率的分母（ADR-0033 §3）。

## 它不装什么

不装原文、不装 ``trace_id``、不装 ``chunk_ids``：这张表是**账**，追溯单次调用要回
``usage_events``。也不装成本/Token——仓库里没有任何 provider 侧的计量，硬算只能靠估算冒充事实。
"""

from datetime import date

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: 直方图的 7 档上界（毫秒）。最后一档没有上界。
LATENCY_BUCKETS_MS: tuple[int, ...] = (200, 400, 600, 800, 1000, 1500)
#: 桶的数量：6 个上界 + 1 个「> 1500」。
LATENCY_BUCKET_COUNT = len(LATENCY_BUCKETS_MS) + 1


def bucket_of(latency_ms: int) -> int:
    """耗时落在第几档（0 起）。边界归下一档：正好 200 ms 算第 1 档。"""

    for index, upper in enumerate(LATENCY_BUCKETS_MS):
        if latency_ms < upper:
            return index
    return LATENCY_BUCKET_COUNT - 1


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    #: UTC 日（与配额窗口同一个换天口径，见 ADR-0033）。
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: ``""`` = 历史汇总（第 14 步之前的行没有 key_id）。
    key_id: Mapped[str] = mapped_column(String(36), primary_key=True, default="")
    #: ``""`` = 历史汇总（那时也没有 endpoint 语义）。
    endpoint: Mapped[str] = mapped_column(String(24), primary_key=True, default="")
    #: 业务码；0 = 成功。
    code: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)

    #: 这一格的请求数（含被拒）。
    requests: Mapped[int] = mapped_column(Integer, default=0)
    #: 其中成功（``code = 0``）的次数。rollup 时一次算好，省得每次查询再 filter。
    ok_requests: Mapped[int] = mapped_column(Integer, default=0)
    #: 三本账合计。
    meter_r: Mapped[int] = mapped_column(Integer, default=0)
    meter_rr: Mapped[int] = mapped_column(Integer, default=0)
    meter_llm: Mapped[int] = mapped_column(Integer, default=0)
    #: 耗时合计与样本数（平均耗时用；分位走下面的直方图）。
    lat_sum_ms: Mapped[int] = mapped_column(Integer, default=0)

    #: 7 档延迟计数。<200 / 200-400 / 400-600 / 600-800 / 800-1000 / 1000-1500 / >1500
    lat_b0: Mapped[int] = mapped_column(Integer, default=0)
    lat_b1: Mapped[int] = mapped_column(Integer, default=0)
    lat_b2: Mapped[int] = mapped_column(Integer, default=0)
    lat_b3: Mapped[int] = mapped_column(Integer, default=0)
    lat_b4: Mapped[int] = mapped_column(Integer, default=0)
    lat_b5: Mapped[int] = mapped_column(Integer, default=0)
    lat_b6: Mapped[int] = mapped_column(Integer, default=0)
