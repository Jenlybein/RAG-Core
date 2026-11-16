"""两种配额共用的越限判定：``used >= limit`` 就拒绝。

日配额（:mod:`app.services.quota_gate`）与静态资源上限（:mod:`app.services.static_quota`）
量的是两样东西——一个是「这一天 / 这一秒已经记了几次」，一个是「库里现在有几行」——
但判据与文案骨架是同一套：``<what> exceeded: <used> used of <limit> <口径>``。
两处各写一遍时，改一次判据（``>=`` 还是 ``>``）就会只改到一条路径，而两条路径恰好都
在拒绝客户。

判据写成 ``>=`` 而不是 ``>``：``used`` 是**已经记下的次数 / 已有的行数**，这次请求 /
这次写入会是第 ``used + 1`` 个。允许它当且仅当 ``used + 1 <= limit``。

判据本身只有一个定义（:func:`has_room`）：拒绝路径与「先问一句」的探测路径
（重排额度用尽时跳过重排）都调它，所以两条路径不可能给出相反的结论。
"""

from __future__ import annotations

from app.core.exceptions import AppError, ErrorCode


def has_room(*, used: int, limit: int) -> bool:
    """同一条判据的**问句形式**：这一次还能不能放行。

    ``used`` 是已经记下的次数 / 已有的行数，所以「还有余量」等价于 ``used + 1 <= limit``。
    拒绝路径（:func:`ensure_under_limit`）与探测路径（``QuotaLedger.has_capacity``）都走
    这里，``>=`` 还是 ``>`` 只在**这一行**决定。
    """

    return used < limit


def ensure_under_limit(
    *,
    used: int,
    limit: int,
    code: ErrorCode,
    what: str,
    suffix: str,
) -> None:
    """额度 N 表示「最多 N 次 / N 行」；达到上限就抛 ``AppError``。

    错误码与句尾口径由调用方给：秒级限流用 ``20005``（该退避重试），配额用尽用
    ``20014``（该提额或换套餐），静态上限再把套餐名缀在句尾——判据只有这一份，
    文案的差异只是把各自的口径如实写出来。
    """

    if not has_room(used=used, limit=limit):
        raise AppError(code, f"{what} exceeded: {used} used of {limit} {suffix}")
