"""节的身份证：``stable_hash(文档 id : 标题路径)``，**不含版本**（ADR-0025 决策五）。

一条切片属于一个「节」，节的身份必须满足两条互相拉扯的性质：

* **带文档 id**：不带的话，两份文档都叫「退款 > 流程」会算出同一个 id，跨文档一对就串
  ——挂到它上面的关系边、按节对比的结果都会悄悄错，且不报错；
* **不含版本**：重建后同一个节必须得到同一个 id，否则「新旧构建逐节对比」根本做不了。
  版本由**引用方**的数据范围表达（边的键是 ``(租户, 库, 版本, 文档 id, 节 id)``），
  不进入 id 本身。所以本模块的函数签名里没有版本参数——不是「传了个空版本」，是
  **没有那个位置**（``tests/test_section_identity.py`` 的模块文档把这条写成了约束）。

## 为什么单独一层

切块器只负责「切」；身份算法必须能被**切块器之外**的地方复用：

* 核对脚本据它验证「库里存的 id 与算法一致」（混了旧算法的版本会在这里露出来）；
* 将来的关系边抽取按它配对两端。

写两份哈希拼接的下场是其中一份迟早漂移，而漂移的表现是「边指向一个存在但不对的节」，
事后极难查。所以算法只有这一份，切块器与核对脚本都从它取名。

## 为什么是 ``:`` 分隔

文档 id 是服务端生成的 UUID（``str(uuid4())``），字符集里没有冒号；标题路径用 ``>``
分段，也没有冒号。两段都不可能含 ``:`` 时，``f"{doc}:{path}"`` 不会出现「换个切法就
撞上」的歧义。这条前提由 ``tests/test_section_identity.py`` 的跨文档用例与
「不同路径不同 id」用例守着。
"""

from __future__ import annotations

import hashlib
from typing import Final

#: 文档 id 与标题路径之间的分隔符（见模块文档「为什么是 ``:`` 分隔」）。
IDENTITY_SEPARATOR: Final[str] = ":"
#: 标题路径里的层级分隔符，与切块器 ``heading_path`` 的写法同源。
PATH_SEPARATOR: Final[str] = " > "


def stable_hash(text: str) -> str:
    """文本的确定性哈希（hex sha256）。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_id_of(*, document_id: str, heading_path: str) -> str:
    """节的身份证：``stable_hash(文档 id : 标题路径)``。

    **关键字参数是刻意的**：两个都是字符串，按位置传参时写反了不会报错，只会得到一个
    同样合法、同样 64 位的哈希——一次静默的身份错位。
    """

    return stable_hash(f"{document_id}{IDENTITY_SEPARATOR}{heading_path}")


def parent_heading_path(heading_path: str) -> str | None:
    """上一级节的标题路径；只有一段（或空路径）时没有父节，回 ``None``。

    与切块器的路径写法共用 ``" > "`` 分隔符：父路径 = 去掉最后一段。**不能用「路径前缀
    匹配」代替**——「退款 > 流程」与「退款 > 流程 > 补充」是父子，而「退款 > 流程」与
    「退款 > 流程说明」不是，字符串前缀分不出这两者。
    """

    if not heading_path:
        return None
    segments = heading_path.split(PATH_SEPARATOR)
    if len(segments) < 2:
        return None
    return PATH_SEPARATOR.join(segments[:-1])


def parent_section_id_of(*, document_id: str, heading_path: str) -> str | None:
    """父节的身份证：**同一个文档 id** + 父路径，走同一个 :func:`section_id_of`。

    父子两侧必须用同一份算法、同一份文档 id：父 id 换一种算法算出来的话，子节指向的
    是一个永远不存在的父节，而症状是「关系边少了一条」——不报错，只是少。
    """

    parent_path = parent_heading_path(heading_path)
    if parent_path is None:
        return None
    return section_id_of(document_id=document_id, heading_path=parent_path)


__all__ = [
    "IDENTITY_SEPARATOR",
    "PATH_SEPARATOR",
    "parent_heading_path",
    "parent_section_id_of",
    "section_id_of",
    "stable_hash",
]
