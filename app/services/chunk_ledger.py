"""第四本账：**每日切块额度**（索引期 embedding，按 token 计）。

## 为什么需要它（ADR-0035 决策三）

前三本账（检索 / 重排 / 大模型）数的都是**请求侧**调用次数。它们看不见索引期真正的成本：
一次整库重建把 5000 篇文档重新向量化，账上一点痕迹都没有。于是「恶意或误操作的重建可以
一天烧光嵌入余额，而所有面板显示今天没怎么用」——这正是这次要堵的那类账。

## 三个东西，各管一件事

| 东西 | 存哪 | 谁读 |
| --- | --- | --- |
| 当日已用量 | Redis 桶 ``quota:chunk:daily:tenant:<tid>:<YYYYMMDD>`` | 门禁（快，热路径） |
| 明细 | ``usage_events.meter_chunk`` + ``endpoint = 'index'`` | 用量页 / 对账 |
| 汇总 | ``usage_daily.meter_chunk`` | 趋势图 |

## 单位是**检索文本 token**，不是正文 token

账单认的是真正发给嵌入模型的那一份：文档名 + 节标题路径 + 节说明前缀 + 正文。它比正文多
10–20%，而单库**容量**判据用的是正文（本地确定、与切块器同一把尺子）。两个口径各管一件
事，混用会让「容量没满但额度超了」这种解释不清的场面出现（ADR-0035 决策一）。

## 门禁在 Worker 里，不在请求路径上

索引是后台动作：上传接口只负责「进队列」。如果额度在请求路径上扣，用户会为「一篇还没开
始解析的文档」被拒——而真正的消耗发生在几分钟之后。所以：

* **上传 / 重建入口**只做「今天额度是不是已经用完了」的预检（便宜、可见）；
* **解析完之后**按真实 token 数记账并硬拦（准确、花在花钱之前）。

## Redis 不可用时怎么办

与前三本账同一条取舍（``quota_gate`` 模块文档）：**不自动降级成无限**。Redis 一挂就所有
租户额度变无限，是本模块最坏的失败模式。所以读不到计数时按 **0** 处理的是「今天的已用量」
（那不冤枉任何人），而**记账失败**会在这里被吞成一条 warning——记账失败不该让一次已经
成功的索引变成失败（切片已经写进了，把它标成失败只会让用户重跑一遍再烧一次钱）。
"""

from __future__ import annotations

from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.models.usage_event import UsageEvent
from app.services.quota_gate import CHUNK_NAMESPACE, QuotaCounters
from app.services.trace_identity import trace_id_for
from app.utils.id_generator import new_retrieval_log_id

logger = structlog.get_logger(__name__)

#: 明细里这一行的端点标记。**必须**用它：用量页的「调用次数 / 成功率 / P50 / P95」全是按
#: 请求行算的，索引行混进去会让成功率莫名其妙地掉、P95 被一个几分钟的批次拉爆。
INDEX_ENDPOINT: Final[str] = "index"

#: 超额时的话术（一因一码：额度这条只有一个原因）。
CHUNK_DAILY_QUOTA_EXCEEDED_MESSAGE: Final[str] = (
    "今日切块额度已用尽：已用 {used} / 上限 {limit} token，当前套餐 {plan}；"
    "明天额度会重置，或找运营把套餐的每日切块额度提上去"
)


def _counters(clock: object = None, counters: QuotaCounters | None = None) -> QuotaCounters:
    """取第四本账的计数器（注入客户端 + 注入时钟，其余全是纯逻辑）。

    ``counters`` 优先（离线套件传内存假实现）：否则每次调用都会去拿进程级的 Redis 客户端，
    而那条路径在无 Redis 的机器上会**建一个绑定到不同事件循环的连接**——症状是
    ``got Future attached to a different loop``，一个与业务毫无关系的报错。

    每次调用新建一个对象：它只握着进程级唯一的 Redis 客户端，构造是廉价的。
    """

    if counters is not None:
        return counters
    from app.core import redis_client

    return QuotaCounters(redis_client.get_redis_client(), namespace=CHUNK_NAMESPACE, clock=clock)  # type: ignore[arg-type]


async def chunk_tokens_today(
    *, tenant_id: str, clock: object = None, counters: QuotaCounters | None = None
) -> int:
    """今天这个租户已经用了多少切块 token（读不到按 0）。

    开关关着时回 0：那时**平台本来就没有在数**，回 0 是「已知的 0」而不是编造——
    ``read_tenant_daily_usage`` 回 ``None`` 是为了让面板能显示「未统计」，而这里是个
    判据需要的数字，判据不能拿到 None。
    """

    if not settings.RATE_LIMIT_ENABLED:
        return 0
    active = _counters(clock, counters)
    window = active.window()
    return await active.daily(level="tenant", ident=tenant_id, window=window)


async def ensure_chunk_daily_quota(
    *,
    tenant_id: str,
    limit: int,
    plan_name: str,
    clock: object = None,
    counters: QuotaCounters | None = None,
) -> None:
    """**入口预检**：今天的切块额度是不是已经用尽了（#222 / #220 的第二处预检）。

    ## 为什么入口要预检一次

    Worker 里的硬拦（``charge_chunk_tokens`` 的「先判后记」）才是真判据——但那时文档已经
    解析完、排过队了。额度早就用尽的租户每上传一篇就白排一次队、白解析一份文件，而用户等
    两分钟只等到一句「今日额度已用尽」。

    这与单库 token 容量的两道门是同一个形状（入口预检 + 解析后硬拦）：一个挡在代价最低处，
    一个给准确判据。**两次都有，缺一不可。**

    ## 为什么预检不预扣

    解析前**不知道这一篇会花多少**（切多少片、检索文本多长）。预扣一个猜的数字等于编造
    ——记真账的纪律（ADR-0018「先判后记」）不允许这么做。所以这里只回答「今天是不是已经
    用尽了」，答「否」就放它进去，真账在 Worker 里记。

    ## 为什么额度用完和「库满」必须是两句话

    「删掉旧文档就能继续」与「只能等明天重置」是两种完全不同的处置。混成一句通用的
    「配额已用尽」，用户会去删文档——而删多少都没用（额度是按**已消耗**记的，不随删除
    回退）。这是票据 #219 分码的原始理由。
    """

    used = await chunk_tokens_today(tenant_id=tenant_id, clock=clock, counters=counters)
    if used < limit:
        return
    raise AppError(
        ErrorCode.CHUNK_DAILY_QUOTA_EXCEEDED,
        CHUNK_DAILY_QUOTA_EXCEEDED_MESSAGE.format(
            used=_format_tokens(used), limit=_format_tokens(limit), plan=plan_name
        ),
        data={
            "reason": "chunk_daily_tokens",
            "used": used,
            "limit": limit,
            "plan": plan_name,
        },
    )


def _format_tokens(value: int) -> str:
    """把 token 数写成「12.4 万」这种量级说法。

    精确到个位在「3 万 token 的上限」面前是噪音：用户要判断的是「还剩多少空间」，不是
    「还差 137 个」。与前端 ``formatCount`` 的取舍一致（那边也是量级 + 两位小数）。
    """

    if value >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return str(value)


async def charge_chunk_tokens(
    db: AsyncSession,
    *,
    tenant_id: str,
    tokens: int,
    limit: int,
    plan_name: str,
    plan_code: str,
    clock: object = None,
    counters: QuotaCounters | None = None,
) -> int:
    """把这次的切块消耗记进第四本账；**超额就抛**（20037）。

    ## 为什么「先读、再判、再写」而不是原子脚本

    与前三本账同一条取舍（``quota_gate`` 模块文档「读-判-写不是原子」）：并发窗口里多记一
    点、让某一次超额请求擦边通过，比在索引热路径上引一段 Lua 更划算——代价是「可能超出
    上限一点点」，收益是「这条路径不会因为脚本写错而卡死所有索引」。而它是**绝对上限**：
    超额那一次本来就不该开始。

    ## 记账与判决的顺序

    先判后记（与 ADR-0018「先判、后记账」一致）：被拒的这一批一次都不扣额度，否则「拒绝
    之后额度更少了」会让用户无论等多久都恢复不了。

    ## 返回值

    这次记账之后当天累计用了多少（调用方写日志 / 回执要用）。
    """

    # 与前三本账同一条取舍（``quota_gate`` 模块文档）：``RATE_LIMIT_ENABLED`` 关着时整段是
    # no-op——**既不读也不写 Redis**。默认套件因此不需要 Redis，也不会因为一条「反正不用
    # 也先连上」的路径在无 Redis 的机器上卡住。
    if not settings.RATE_LIMIT_ENABLED:
        return 0

    amount = max(int(tokens), 0)
    if amount == 0:
        # 空正文不值得记一笔：它既不加额度，也不该在账上留下一条 0。
        return await chunk_tokens_today(tenant_id=tenant_id, clock=clock, counters=counters)

    active = _counters(clock, counters)
    window = active.window()
    used = await active.daily(level="tenant", ident=tenant_id, window=window)
    projected = used + amount
    if projected > limit:
        raise AppError(
            ErrorCode.CHUNK_DAILY_QUOTA_EXCEEDED,
            CHUNK_DAILY_QUOTA_EXCEEDED_MESSAGE.format(
                used=used, limit=limit, plan=f"{plan_name}（{plan_code}）"
            ),
            # ``data`` 是**仅关键字**参数：写成位置参数会在这一行抛 TypeError，
            # 于是「额度用尽 → 20037」变成「额度用尽 → 500」。mypy 抓到的就是这个。
            data={"reason": "chunk_daily_tokens", "used": used, "limit": limit, "plan": plan_code},
        )

    # 桶：按量 +（不是 +1）。
    await active.add_daily(level="tenant", ident=tenant_id, window=window, by=amount)

    # 明细 + 汇总。两处都写，因为用量页读明细、趋势图读汇总——只写一处会让其中一个页面
    # 永远看不见这项消耗（而本票要解决的正是「看不见」）。
    # ``trace_id`` 在明细行上是唯一且非空的。索引没有一次「检索的 trace」可继承，但**确定性
    # 派生**这条纪律仍要守（ADR-0021）：log_id 新生成，trace_id 由它派生，于是「按 trace_id
    # 反查这一行」在索引这侧也成立。
    log_id = new_retrieval_log_id()
    db.add(
        UsageEvent(
            id=log_id,
            trace_id=trace_id_for(log_id),
            tenant_id=tenant_id,
            # 汇总行不带 Key：索引是后台动作，没有「哪把 Key 发起的」这个概念（ADR-0035）。
            key_id=None,
            endpoint=INDEX_ENDPOINT,
            code=0,
            meter_chunk=amount,
            # ``top_k`` 是 NOT NULL（它是请求侧「这次要了几条」的读数）。索引根本不是一次
            # 检索请求，如实值应该是「不存在」——但列不允许 NULL，于是填 **0**：它是这个
            # 列上唯一诚实的值（「这一行没有要几条这回事」），而不是随手填个 5 或 10 让人
            # 误读成「索引按 top_k=10 召回了 10 条」。
            #
            # 这条在离线套件里**看不见**（``tests/fake_db.py`` 不模拟 NOT NULL），第一次
            # 真跑索引才炸出来：``NotNullViolation: null value in column "top_k"``。
            top_k=0,
            # ``latency_ms`` 同样是 NOT NULL。索引不是一次请求，没有「这一次花了多少毫秒」
            # 可言；而写 0 是诚实的（不是「很快」，是「这个量在这一行上不成立」）。真正该
            # 关心耗时的地方是构建行的收敛期限，不是这里。
            latency_ms=0,
            # 这两个 jsonb 也是 NOT NULL（模型给了 ``default=dict``，但**那只是 Python 侧
            # 的默认**，SQL 层没有 server_default——批量插入或直接构造时漏了就是 NOT NULL 违反）。
            kb_ids=[],
            chunk_ids=[],
        )
    )
    await _bump_daily(db, tenant_id=tenant_id, amount=amount)

    logger.info(
        "chunk tokens charged",
        tenant_id=tenant_id,
        tokens=amount,
        used_today=projected,
        limit=limit,
    )
    return projected


async def _bump_daily(db: AsyncSession, *, tenant_id: str, amount: int) -> None:
    """汇总行 ``usage_daily.meter_chunk`` +amount（当天行不存在就建一行）。

    用一条 ``INSERT ... ON CONFLICT DO UPDATE``：读-改-写在这里会与并发的另一批索引任务
    撞车，而丢的那部分恰好是「这个租户今天花了多少」——一个不该有误差的数字。
    """

    from sqlalchemy.dialects.postgresql import insert

    from app.models.usage_daily import UsageDaily

    moment = now()
    statement = (
        insert(UsageDaily)
        .values(
            # UTC 日（与配额窗口同一个换天口径，ADR-0033）。
            day=moment.date(),
            tenant_id=tenant_id,
            # 汇总行不带 Key：索引是后台动作，没有「哪把 Key 发起的」这个概念（ADR-0035）。
            key_id="",
            endpoint=INDEX_ENDPOINT,
            code=0,
            requests=1,
            ok_requests=1,
            meter_chunk=amount,
        )
        .on_conflict_do_update(
            # 与表上的主键逐列一致：漏一列就会插出重复行，而「今天用了多少」就不再是一个数。
            index_elements=[
                UsageDaily.day,
                UsageDaily.tenant_id,
                UsageDaily.key_id,
                UsageDaily.endpoint,
                UsageDaily.code,
            ],
            set_={
                "requests": UsageDaily.requests + 1,
                "ok_requests": UsageDaily.ok_requests + 1,
                "meter_chunk": UsageDaily.meter_chunk + amount,
            },
        )
    )
    await db.execute(statement)
