"""两种配额共用的越限判定：``used >= limit`` 就拒绝。

日配额（:mod:`app.services.quota_gate`）与静态资源上限（:mod:`app.services.static_quota`）
量的是两样东西——一个是「这一天 / 这一秒已经记了几次」，一个是「库里现在有几行」——
但判据与文案骨架是同一套：``<what> exceeded: <used> used of <limit> <口径>``。
两处各写一遍时，改一次判据（``>=`` 还是 ``>``）就会只改到一条路径，而两条路径恰好都
在拒绝客户。

判据写成 ``>=`` 而不是 ``>``：``used`` 是**已经记下的次数 / 已有的行数**，这次请求 /
这次写入会是第 ``used + 1`` 个。允许它当且仅当 ``used + 1 <= limit``。
"""

from __future__ import annotations

from app.core.exceptions import AppError, ErrorCode


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

    if used >= limit:
        raise AppError(code, f"{what} exceeded: {used} used of {limit} {suffix}")
