"""数据范围关卡：``kb_ids ⊆ 密钥配置的 allowed_kb_ids``（六道关卡的第二道）。

**为什么必须只有一个函数**：白名单要在每一个「按名指定知识库」的数据面入口上判——
检索、列文档、上传、重建、删除与两个 MCP 工具。逐个入口各写一遍 ``if kb not in
allowed`` 的下场是可以预料的：某一次重构只改了其中几处，剩下那处就成了绕过白名单的
口子，而测试未必覆盖到它。判定只有这一份，入口只负责「把这次要查的库递进来」。

**越界与跨租户统一回「知识库不存在」（``20008``），绝不回「无权限」**：可区分的拒绝
等于回答「这个资源存在，只是不归你」，攻击者拿它一个一个试就能把别人的库标识摸出来。
跨租户的判定有两层（这里按白名单、下游按库行的 ``tenant_id``），但两层对外是同一个
错误码，所以多一层不改变调用方看到的东西。

**空白名单 = 什么都不许（失败关闭）。** 仓库里「空列表」在别处表示「还没圈定」，
若这里把它读成「全部」，那么清空一份白名单就成了开闸——一次误操作就从「这把 Key 只能
查两个库」变成「能查全租户」。空就是空。

**唯一例外是内置开发身份**，而且这个例外由快照上的显式布尔 ``unrestricted_scope``
表达，不是靠一条空列表兼职表达两件事（T3 的上下文已经点出这个坑）：真实配置映射出来
永远是受限的，只有合成的开发快照把它打开。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import KeyPolicySnapshot


def ensure_kb_scope(*, policy: KeyPolicySnapshot, kb_ids: Sequence[str]) -> None:
    """校验这次要查的库都在这把 Key 的范围内；否则抛「知识库不存在」（``20008``）。

    调用位置是**第二道关卡**（ADR-0018：鉴权 → 白名单 → 套餐 → 档位 → 限流 → 配额）：
    身份已经解出来了（``policy`` 就在手上），而限流、配额与召回都还没开始。反过来
    （先扣额度再判越权）会让攻击者拿一个越界的 ``kb_id`` 连打就把客户当天的额度打光。

    一次请求里只要有一个库越界，**整句请求**就拒绝，不做「摘掉越界的、剩下照查」：
    那样调用方拿到的是缺了库的结果，却看不出自己撞了墙。
    """

    if policy.unrestricted_scope:
        return

    allowed = set(policy.allowed_kb_ids)
    if not allowed:
        # 空白名单：这把 Key 一个库都不许查，连「什么都不查」的请求也不放行——一个
        # 只能被空请求满足的关卡，等于在「什么都不知道」的时候说「行」。
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    if any(kb_id not in allowed for kb_id in kb_ids):
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)


def scoped_kb_ids(*, policy: KeyPolicySnapshot, kb_ids: Iterable[str]) -> list[str]:
    """按同一套范围**过滤**（列库用），顺序原样保留。

    列库与检索用的是同一个判定，只是反应不同：检索撞到范围外的库必须拒绝（那是
    「这次要查什么」的一部分），列库则只把范围外的库从清单里去掉——清单本身就是
    「你能看哪些库」的答案，为一条不可见的库把整个清单变成 20008 没有意义。
    """

    if policy.unrestricted_scope:
        return list(kb_ids)

    allowed = set(policy.allowed_kb_ids)
    return [kb_id for kb_id in kb_ids if kb_id in allowed]
