"""两种配额共用的越限判定：``used >= limit`` 就拒绝。

日配额（:mod:`app.services.quota_gate`）与静态资源上限（:mod:`app.services.static_quota`）
量的是两样东西——一个是「这一天 / 这一秒已经记了几次」，一个是「库里现在有几行」——
但判据同一套：``used >= limit`` 就拒绝。

判据写成 ``>=`` 而不是 ``>``：``used`` 是**已经记下的次数 / 已有的行数**，这次请求 /
这次写入会是第 ``used + 1`` 个。允许它当且仅当 ``used + 1 <= limit``。

判据本身只有一个定义（:func:`has_room`）：拒绝路径与「先问一句」的探测路径
（重排额度用尽时跳过重排）都调它，所以两条路径不可能给出相反的结论。

## 文案由调用方给，骨架不共用

改造前这里还负责拼一句共用的英文骨架（``<what> exceeded: <used> used of <limit>``）。
**那正是 #219 要修的病**：四种完全不同的上限（库数满 / 文档数满 / 按天额度用尽 /
已被删除的并发名额）共用一句话，于是对其中三种都是错话——把「删掉旧文档就能继续」
说成「等额度重置」，把「要运营改套餐」说成「去密钥配置里调整」。

共用判据是**正确性**（``>=`` 还是 ``>`` 只能有一处答案），共用文案却是**产品错误**
（不同的处置不该长成同一句话）。所以现在：判据留在这里，码、文案、机器可读的
``data`` 全部由调用方按「这是哪一种上限」给（见 :mod:`app.services.static_quota` 的
``CapacityQuota`` 表）。
"""

from __future__ import annotations

from typing import Any

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
    message: str,
    data: Any = None,
) -> None:
    """额度 N 表示「最多 N 次 / N 行」；达到上限就抛 ``AppError(code, message, data)``。

    三个入参由调用方按**这一种**上限给：

    * ``code``：专属业务码。容量型各有一条自己的码（``20034`` / ``20035`` / ``20036``），
      因为调用方要做的处置各不相同；按天调用额度仍是 ``20014``。
    * ``message``：给用户看的那句话。容量型是中文人话（带已用 / 上限 / 套餐与处置），
      日配额那边沿用英文骨架（那是运维日志语境）。
    * ``data``：机器可读的原因与数字（``reason`` / ``used`` / ``limit`` / ``plan``），
      面板与客户端据此渲染「已用 X / 上限 Y」，不必去解析文案。
    """

    if not has_room(used=used, limit=limit):
        raise AppError(code, message, data=data)
