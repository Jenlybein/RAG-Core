"""三条模型端点的**前置硬边界**：只有「去掉标点后什么都不剩」这一类。

## 为什么这里只剩一条规则

这一块最初写了一套字符规则（感叹词表、单字符重复、噪声占比），用来在没有模型判断的
情况下识别「无意义问题」。实测两次就被证否：

* ``啊啊啊啊…a``（末尾多一个拉丁字母）三条规则全部放过，照样返回 5 条无关切片；
* 为了补上它加的「噪声占比 0.85」，紧接着把 ``E1025`` 判成无内容——每个字符都只出现
  一次，「出现最多的字符」占比算出来是 1.0。

每补一个漏报就开一个新的误报口子，这是逐条判的必然结果：它只看「字符长什么样」，
不看「这句话想干什么」。**语义判断交给模型**（见下面的「判定在哪里发生」）。

## 判定在哪里发生（不是这里）

三条端点**本来就要调一次模型**，意图识别做成那一次结构化调用的一个字段，不另发一次：

* ``/rewrite`` → :class:`app.services.query.rewrite.QueryRewrite` 的 ``intent`` 字段；
* ``/decompose`` 与 ``/research`` → :class:`app.services.research.ResearchPlan` 的
  ``intent`` 字段（两者共用同一个 Planner 与同一份提示词）。

模型说 ``no_content`` 时，端点抛 ``20033``，在**任何嵌入、检索、重排、记账之前**停下。
``intent`` 字段缺省是 ``search``：模型没给、格式坏了、老调用点没传，一律按「有内容」
继续跑——判定失败不该变成「所有请求都被拒」。

## 这条规则为什么还留着

它不涉及语义，也没有争议：``？`` / ``🙂`` / ``   `` 这种输入连一个字都没有，让模型
去判只是白花一次调用的时间。它挡掉的是**结构上不可能有问题**的输入，不含任何猜测。
"""

from __future__ import annotations

import unicodedata

from app.core.exceptions import AppError, ErrorCode

#: 算作「内容」的字符类别之外的一切都先被剔掉。Unicode 大类：
#: ``P`` 标点、``S`` 符号（含 emoji）、``Z`` 分隔符、``C`` 控制 / 未分配 / 格式字符。
_IGNORED_CATEGORIES = frozenset("PSZC")


def content_chars(query: str) -> str:
    """只留可能承载含义的字符：NFKC 归一之后去掉标点、符号、空白与控制字符。

    NFKC 是必要的：全角 ``？？？`` 与半角 ``???``、全角字母与半角字母在归一后一致，
    否则同一句输入会因为输入法不同判出两种结果。
    """

    normalized = unicodedata.normalize("NFKC", query)
    return "".join(
        char for char in normalized if unicodedata.category(char)[0] not in _IGNORED_CATEGORIES
    )


def has_no_content(query: str) -> bool:
    """去掉标点 / 符号 / 空白之后一个字符都不剩。**这是这条闸的全部逻辑。**"""

    return content_chars(query) == ""


def ensure_query_has_content(query: str) -> None:
    """只剩标点时抛 ``20033``；其余情况一律放行，交给模型的 ``intent`` 判定。

    这里刻意**不**判「像不像垃圾」：那是语义问题，字符规则判不准（模块文档记了两次
    实测的漏判与误杀）。
    """

    if not has_no_content(query):
        return
    raise AppError(
        ErrorCode.QUERY_HAS_NO_CONTENT,
        "query has no searchable content: only punctuation, symbols or whitespace",
    )


__all__ = ["content_chars", "ensure_query_has_content", "has_no_content"]
