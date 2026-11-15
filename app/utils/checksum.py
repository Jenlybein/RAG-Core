"""正文的规范化与校验和：**判重口径的唯一入口**（ADR-0005 D8 / #172）。

同一份正文可能有几种写法落盘（Windows 与 Unix 的换行、输入法给的 NFC 与 NFD、
手滑多敲的首尾空格）。判重、版本行的校验和、迁移回填如果各算一套，迟早出现
「两个函数都自称 sha256、对同一份正文给出不同值」——而症状是同一份内容被当成改稿
灌进一个新稿次，或者一次真改动被判成「内容没变」，两者都不报错。

所以规范化只有 :func:`normalize_text` 一份，校验和只有 :func:`checksum_of` 一份，
调用点一律走这里，**不许**在 SQL 里用数据库的 ``digest()`` / ``pgcrypto`` 顺手算一个
（那等于把「规范化到底磨了哪几样」写成两份，且两份会在某次改动里分叉）。

## 规范化到底磨了哪三样

1. 换行：``\\r\\n`` 与 ``\\r`` 统一成 ``\\n``（文件从 Windows 拷过来不算改稿）；
2. Unicode：整串折到 NFC（同一个带重音字符的两种码位序列是同一个字符）；
3. 首尾空白去掉（``strip()``，含首尾空行）。

**别的都不动**：中间的空格、缩进、换行**位置**、大小写、全半角一律原样计分。
多磨一刀的代价是把一次真改动判成「没变」，那是静默丢一次改稿；少磨一样只是判重偏严。
两端不对称，所以选择偏严的那一侧。

## 迁移回填为什么必须调这个函数

#172 的迁移给存量文档逐行建版本行。校验和若在数据库里算（``digest(content,'sha256')``），
存量行与新写入的行就成了两套规范化：一份磨了换行与 NFC、另一份没磨，之后「同一个正文
算出两个校验和」——判重从此永远判不出来。迁移因此用 Python 逐行调 :func:`checksum_of`，
代价是这条迁移不能走 ``alembic upgrade --sql`` 离线渲染（渲染时没有库可读）。
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Final

#: 校验和的长度：``sha256`` 的十六进制位数，也是数据库列的定长（``String(64)``）。
CHECKSUM_HEX_LENGTH: Final[int] = 64


def normalize_text(text: str) -> str:
    """把正文磨成「算校验和与判重用的那一份」（磨哪三样见模块文档）。"""

    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", unified).strip()


def checksum_of(text: str) -> str:
    """规范化后的正文的 ``sha256`` 十六进制摘要（64 位小写）。

    空正文是合法正文（没有切片的失败文档允许没有正文），于是这里不拒绝空串：它有一个
    稳定且众所周知的摘要，对账与判重都用得上。
    """

    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
    assert len(digest) == CHECKSUM_HEX_LENGTH, "sha256 的十六进制摘要恒为 64 位"
    return digest


__all__ = ["CHECKSUM_HEX_LENGTH", "checksum_of", "normalize_text"]
