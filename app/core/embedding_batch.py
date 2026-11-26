"""百炼 embedding 的「单批上限」表（票 #224）。

## 为什么这张表要独立成一个模块

单批上限是**供应商事实**，不是本项目的偏好：一批塞超过它，接口回 400，而 Provider 层
没有批级重试（一批失败就整篇抛 ``EMBEDDING_FAILED``，任务层是「整篇重跑 ×2、间隔 10 秒」），
所以一个填错的批大小，代价是同一篇文档白烧三遍 embedding。这份知识今天散在
``docs/架构.md``、``docs/技术栈.md`` 与两个 Provider 的模块文档字符串里，面板却完全看不见。

票 #224 之后有三处要读它，必须读同一份：

1. 运行时配置的**上界校验**（``SETTING_SPECS["EMBEDDING_BATCH_SIZE"].maximum``）——
   面板写进来的值不能超过任何一个受支持模型的上限，否则「保存成功、跑起来才 400」；
2. 面板上的**建议值**（「当前模型 X（单批上限 Y），建议 ≤ Y」）——按当前生效模型给，
   因为它才是决定一次 HTTP 请求能塞几条切片的那个模型；
3. 生产文档里那张表（``docs/架构.md``）——它抄的就是这里的值，改这里要一起改文档。

上界取 :data:`MAX_EMBEDDING_BATCH_SIZE` 而不是「当前模型的上限」：当前模型可以在同一天里
被换掉（``EMBEDDING_MODEL`` 也是运行时配置），把上界绑死在一个**可变的**值上，会让
「昨天合法的 20、今天换模型后变成非法」这种事后翻案悄悄发生。硬上界取所有受支持模型里
最宽松的那个（25），是按「永远不会被任何一个受支持模型拒绝」这个条件算出来的；具体该用
多少，交给面板上的建议值与管理员判断。
"""

from __future__ import annotations

from typing import Final

#: 模型名（小写）→ 单批上限。数值来自百炼文档；``docs/架构.md`` 的 ``embedding/`` 那一行
#: 是同一张表的文字版。
EMBEDDING_BATCH_LIMITS: Final[dict[str, int]] = {
    "text-embedding-v1": 25,
    "text-embedding-v2": 25,
    "text-embedding-v3": 10,
    "text-embedding-v4": 10,
    "qwen3.7-text-embedding": 20,
}

#: 认不出模型时按最保守的值走：宁可贵一点（多几次请求），也不要一次 400 把整篇文档打回。
DEFAULT_EMBEDDING_BATCH_LIMIT: Final[int] = 10

#: 白名单里这一项的上界：受支持模型中最宽松的那个。见模块文档「上界取哪个值」。
MAX_EMBEDDING_BATCH_SIZE: Final[int] = max(EMBEDDING_BATCH_LIMITS.values())


def batch_limit_of(model: str) -> int:
    """这个模型的单批上限；认不出来时给最保守的 :data:`DEFAULT_EMBEDDING_BATCH_LIMIT`。

    先精确匹配、再按「模型名里最长的那条已知名」匹配：真实配置里常带供应商前缀或快照
    后缀（``qwen/text-embedding-v3``、``text-embedding-v3-20250301``），而 ``v1`` 与
    ``v3`` 这类短名互相并不包含，取最长命中就够了，不需要正则。
    """

    name = (model or "").strip().lower()
    if not name:
        return DEFAULT_EMBEDDING_BATCH_LIMIT
    exact = EMBEDDING_BATCH_LIMITS.get(name)
    if exact is not None:
        return exact

    matched = [known for known in EMBEDDING_BATCH_LIMITS if known in name]
    if not matched:
        return DEFAULT_EMBEDDING_BATCH_LIMIT
    return EMBEDDING_BATCH_LIMITS[max(matched, key=len)]
