"""关键词索引的**版本化物理命名**与别名切换动作（纯函数，不碰客户端）。

ADR-0025 决策二：关键词索引**按版本物理隔离**，用一个别名指向 active。

```text
rag_chunks              ← 别名（检索永远只认它）
rag_chunks__v1          ← 物理索引（v1 的那批文档，active 时被别名指着）
rag_chunks__v2          ← 物理索引（构建中的新版本，此时无人指向它）
```

为什么不在一张索引里双写两个版本（那是被否掉的做法）：

* **回滚只能是删文档**，而 ES 的删除是标记删除，不立刻省空间；
* **任何一条漏带版本条件的查询都会同时捞到两个版本的行**——那正是 ADR-0025 决策四
  要防的「不报错但配对错了」。

为什么别名切换放在这里（而不是让检索改查物理名）：检索侧只认配置里的那一个名字
（``ELASTICSEARCH_INDEX``），它现在是**别名**。激活时把别名从旧物理索引挪到新物理
索引，检索代码一行都不用改；切换前它查的仍是旧索引——「别名未切换前检索仍查旧物理
索引」不是靠约定，是靠名字解析本身。

命名形状 ``<别名>__<版本>`` 的 ``__`` 是刻意的分隔：``version_index_pattern`` 用
``<别名>__*`` 能覆盖到全部版本，而**不会**误伤 ``rag_chunks_eval`` 这种评测专用索引
（它跟别名不是同一条前缀）。
"""

from __future__ import annotations

from typing import Final

#: 别名与物理索引之间的分隔符。
VERSION_SEPARATOR: Final[str] = "__"


def physical_index_name(alias: str, version: str) -> str:
    """``(rag_chunks, v2)`` → ``rag_chunks__v2``。

    同一个 (别名, 版本) 永远得到同一个名字：构建、激活、删除三处都从这一个函数取名，
    任何一处手写字符串都会在某一刻与另外两处错开（而错开的表现是「写进了没人查的索引」，
    不报错，只是搜不到）。
    """

    return f"{alias}{VERSION_SEPARATOR}{version}"


def version_index_pattern(alias: str) -> str:
    """``rag_chunks`` → ``rag_chunks__*``：这个别名的**全部版本**物理索引。"""

    return f"{alias}{VERSION_SEPARATOR}*"


def version_from_physical_index(alias: str, index_name: str) -> str | None:
    """``(rag_chunks, rag_chunks__v2)`` → ``v2``；不是这个别名的物理索引回 ``None``。"""

    prefix = f"{alias}{VERSION_SEPARATOR}"
    if not index_name.startswith(prefix):
        return None
    version = index_name[len(prefix) :]
    return version or None


def build_alias_actions(
    *, alias: str, target: str, current: tuple[str, ...] | list[str]
) -> list[dict[str, dict[str, str]]]:
    """拼出 ``indices.update_aliases`` 的 ``actions``：**一次调用里**完成切换。

    返回空列表表示「别名已经指向目标索引」——这时一个请求都不该发（重复激活必须是
    幂等的 no-op，否则每次激活都会多一次可能失败的写）。

    为什么先 ``remove`` 再 ``add`` 是**同一次调用**里的两个 action：ES 的
    ``update_aliases`` 在一个集群状态更新里连续应用它们，中间不存在「别名指向零个索引」
    或「指向两个索引」的时刻。分两次调用就会各有一个这样的窗口。
    """

    if target in current and len(current) == 1:
        return []
    actions: list[dict[str, dict[str, str]]] = [
        {"remove": {"index": index, "alias": alias}}
        for index in current
        if index != target
    ]
    if target not in current:
        actions.append({"add": {"index": target, "alias": alias}})
    return actions
