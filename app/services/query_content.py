"""「这条问题里有没有可检索的内容」——三条模型端点的前置闸。

## 为什么要有这道闸

模型端点（``/rewrite`` / ``/decompose`` / ``/research``）在无意义输入上不会失败，它会
**编**。真实见过的一次：问 ``啊啊啊啊啊啊啊``，分解器洋洋洒洒给出四个「要点」——
「输入中没有实义词，无法确定具体的知识需求」「连续感叹词通常反映情绪激动或焦虑状态」
……然后拿着这几条自述去召回，返回一堆与业务毫无关系的切片。调用方拿到的是
``code = 0`` 和五条材料，**看不出这些材料和自己的问题没有关系**。

那既不是模型的错（它正确地说了「无法确定需求」），也不是检索的错（它老老实实按那几句
查了）。错在**这一段根本不该开始**：没有内容的问题不该产生检索结果，更不该花掉一次大
模型调用与一次额度。

所以这道闸放在**任何模型调用之前**、配额检查之前——被它拒掉的请求一次都不扣额度
（ADR-0018「进门只查、成功后记」）。失败码是 ``20033 QUERY_HAS_NO_CONTENT``：一个明确
的业务失败，调用方据此知道该改问题，而不是以为服务坏了。

## 判定为什么是词法而不是模型

判定只用**字符类别**与一张很短的感叹词表，三个理由：

1. **要在任何模型调用之前就能判定**——用模型判「有没有内容」，本末倒置（那正是要省的
   那次调用）；
2. **要确定**：同一个问题在两次调用上判出不同答案，就没法写测试、也没法解释；
3. **宁可漏判，不可误判**：把一条真实问题判成「无意义」的代价，比放过一条垃圾输入高得
   多——前者会让付钱的用户以为服务坏了。所以下面只挡三类**结构上**就不可能是问题的输入。

## 三类被判为「无内容」

1. **去掉标点 / 符号 / 空白后什么都不剩**（``？？？`` / ``... `` / ``🙂``）；
2. **内容字符全是感叹词或象声词**（``啊啊啊啊`` / ``哈哈哈`` / ``嗯嗯嗯``）——这张表是
   一个封闭清单，见 :data:`INTERJECTION_CHARS`，不含任何实词；
3. **内容字符只有一个、且重复了两遍以上**（``xxxx`` / ``aaaa`` / ``。。。``）——单个重复
   字符构不成一个词。

刻意**不做**的事：

* 不按长度判（``E1025`` 这种短查询是完全有效的问题）；
* 不用关键词检索那张查询侧停用词表（``QUERY_STOPWORDS``）反过来判「整句只剩虚词」：
  那张表里有 ``一般`` 这类既是填充词又是业务术语的条目，拿它当拒绝条件会误杀真实问题
  （那张表自己的注释里就写着这个反例）。
"""

from __future__ import annotations

import unicodedata

from app.core.exceptions import AppError, ErrorCode

#: 感叹词 / 象声词：整条问题只由这些字构成时，它没有可检索的内容。
#:
#: **封闭清单**，进这张表的条件只有一个：它本身不承载任何业务含义。任何可能构成实词的
#: 字（``打`` / ``退`` / ``款`` …）都不能进来——误判一条真实问题比放过一条垃圾输入更糟。
INTERJECTION_CHARS: tuple[str, ...] = (
    "啊",
    "阿",
    "哦",
    "噢",
    "喔",
    "噢",
    "呃",
    "嗯",
    "唉",
    "哎",
    "呀",
    "哈",
    "嘿",
    "呵",
    "呜",
    "嗷",
    "嘤",
    "哇",
    "咦",
    "耶",
    "咯",
    "喽",
    "呗",
    "嘛",
    "呸",
    "嘘",
    "哼",
    "咚",
    "砰",
)

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


def is_meaningless(query: str) -> bool:
    """这条问题有没有可检索的内容（判定口径见模块文档的三类）。"""

    content = content_chars(query)
    if content == "":
        # 1. 只剩标点 / 符号 / 空白。
        return True
    if all(char in INTERJECTION_CHARS for char in content):
        # 2. 全是感叹词 / 象声词。
        return True
    # 3. 只有一个字符、且重复两遍以上。
    return len(set(content)) == 1 and len(content) >= 2


def ensure_query_has_content(query: str) -> None:
    """过了就返回；没过就抛 ``20033``。

    文案说清两件事：**这不是服务故障**（同一把 Key 换个问法就能用），以及**该怎么改**
    （说出你要查的东西）。控制台的 ``CODE_COPY`` 会给同一件事的人话版本。
    """

    if not is_meaningless(query):
        return
    raise AppError(
        ErrorCode.QUERY_HAS_NO_CONTENT,
        "query has no searchable content: only punctuation, symbols or interjections",
    )


__all__ = ["INTERJECTION_CHARS", "content_chars", "ensure_query_has_content", "is_meaningless"]
