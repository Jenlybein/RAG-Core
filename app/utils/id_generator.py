"""ID 生成收口：所有由中台生成的 ID 都从这里出。

    tenant_id / user_id 是调用方传入的；kb_id / document_id / chunk_id /
    retrieval_log_id / policy_id 由中台生成（UUID v4）；API Key 原文也在这里生成。

不要在各 service 里手写 uuid4()。将来要给 ID 加前缀、或换成雪花算法，
只改这个文件一处。

API Key 的格式（``rc_live_<key_id>.<32 位随机十六进制>``）也收在这里：**生成、拆解、
打码**三件事共用同一组常量，散着写就会各自漂移。

格式里必须带 ``key_id``：库里只存 ``scrypt`` 哈希（见 :mod:`app.core.api_key_credential`），
哈希没法当索引用，所以「这是哪一行」只能由 Key 自己携带——鉴权靠它一次主键命中。
"""

import secrets
import string
import uuid
from typing import Final

#: 密钥部分允许出现的字符。用 ``string.hexdigits`` 而不是正则：这里只回答「是不是十六进制」。
_HEX_DIGITS: Final[str] = string.hexdigits

#: Key 原文的固定前缀。``rc`` = RAG Core；``live`` 与将来的测试环境 Key 区分开。
API_KEY_PREFIX: Final[str] = "rc_live_"
#: 前缀之后是行身份证（``api_keys.id``，UUID），再一个点，最后是密钥本体。
API_KEY_SEPARATOR: Final[str] = "."
#: 密钥部分的十六进制位数（``secrets.token_hex`` 每字节两位，所以要除以 2）。
API_KEY_HEX_LENGTH: Final[int] = 32
#: masked 形态保留末几位。5 位是 20 bit，足以在面板里认清是哪一把；4 位在同一租户
#: 签发十几把之后就开始重复，而两位相同的 Key 在列表里没法区分。
API_KEY_MASK_TAIL: Final[int] = 5
#: masked 形态里的省略号。用单字符「…」而不是三个点：固定宽度截断时不会数错位数。
API_KEY_MASK_ELLIPSIS: Final[str] = "…"
#: 太短、无法「保留头尾仍不泄露」的值整体隐去。空值不在此列（空值没有东西可泄露）。
MASK_HIDDEN: Final[str] = "••••"


def new_id() -> str:
    """生成一个 UUID v4 字符串。"""

    return str(uuid.uuid4())


def mask_secret(value: str, *, keep_head: int = 3, keep_tail: int = 4) -> str:
    """``sk-…abcd``：保留前 ``keep_head`` 位与末 ``keep_tail`` 位，中间截掉。

    这件事现在是**两条路**，因为数据面 API Key 已经拿不到原文了：

    * **还有原文的**（模型接入的 ``api_key``、配置中台里带 ``secret`` 标记的运行时配置）
      走这一个函数；
    * **没有原文的**（``api_keys``）走 :func:`mask_api_key_tail`——它拼的是库里那一截
      ``key_tail``。

    以前三处各写一份「保留几位」，看起来都像 masked，泄露面却各不相同；现在两条路各只有
    一处实现，规则都写在这个文件里。

    值短于 ``keep_head + keep_tail`` 时**整体隐去**（``••••``）：这种情况下保留任何一段
    都接近原文，而按位截断还可能把很短的值整个回显出去。空值原样返回空串——空串没有
    可泄露的内容，也不该在面板上显示成一块黑方块。
    """

    if keep_head < 0 or keep_tail < 0:
        raise ValueError("mask_secret 的保留位数不能为负")

    if not value:
        return ""
    if len(value) <= keep_head + keep_tail:
        return MASK_HIDDEN

    head = value[:keep_head] if keep_head else ""
    tail = value[-keep_tail:] if keep_tail else ""
    return f"{head}{API_KEY_MASK_ELLIPSIS}{tail}"


def api_key_tail_of(secret: str) -> str:
    """密钥本体 → 落库的那一截尾巴（``api_keys.key_tail``）。

    库泄露时它是唯一被看见的部分：20 bit，再连同「前缀 + 行身份证」也不足以重建密钥。
    """

    return secret[-API_KEY_MASK_TAIL:]


def mask_api_key_tail(key_tail: str) -> str:
    """``rc_live_…4f3a2``：前缀（常量）+ 省略号 + 末 5 位。

    **入参是库里那一截尾巴，不是原文**：签发之后原文就不存在了，mask 只能从 ``key_tail``
    拼出来。前缀来自 :data:`API_KEY_PREFIX`，不占列——它是常量，每行存一遍只是把常量
    抄进库里。空尾巴（手写进库的坏数据）整体隐去。
    """

    if not key_tail:
        return MASK_HIDDEN
    return f"{API_KEY_PREFIX}{API_KEY_MASK_ELLIPSIS}{key_tail}"


def new_kb_id() -> str:
    return new_id()


def new_document_id() -> str:
    return new_id()


def new_chunk_id() -> str:
    return new_id()


def new_version_id() -> str:
    """版本行（``document_versions``）的主键（#175）。

    ``(document_id, version)`` 才有业务含义；这一列只是行的身份证，所以用同一套 UUID v4。
    """

    return new_id()


def new_retrieval_log_id() -> str:
    return new_id()


def new_api_key_value(key_id: str) -> str:
    """生成 ``rc_live_<key_id>.<32 位十六进制>`` 的 Key 原文。

    用 :mod:`secrets` 而不是 :mod:`uuid` 生成密钥部分：它是凭证，必须取自操作系统的密码学
    随机源；UUID v4 有 122 位随机性但格式里含固定段，拼进前缀后容易被误当成「可预测的标识」。

    ``key_id`` 由调用方先建好行再传进来：串里带的就是那一行的主键，鉴权因此不必猜
    「这是哪一行」——库里只存哈希，没有可做索引的原文。
    """

    return (
        f"{API_KEY_PREFIX}{key_id}{API_KEY_SEPARATOR}"
        f"{secrets.token_hex(API_KEY_HEX_LENGTH // 2)}"
    )


def split_api_key_value(value: str) -> tuple[str, str] | None:
    """把 Key 原文拆成 ``(key_id, secret)``；形状不对返回 ``None``。

    只认「前缀 + 行身份证 + 点 + 定长十六进制」这一种形状：多一个空格、少一位、混进别的
    前缀都算形状不对，由调用方翻成「格式不对」的错误消息——它和「这把 Key 不存在」
    是两件事，混在一起会让人反复核对一把其实没问题的 Key。
    """

    if not value.startswith(API_KEY_PREFIX):
        return None
    key_id, separator, secret = value[len(API_KEY_PREFIX) :].partition(API_KEY_SEPARATOR)
    if not separator or not key_id or len(secret) != API_KEY_HEX_LENGTH:
        return None
    if any(char not in _HEX_DIGITS for char in secret):
        return None
    return key_id, secret
