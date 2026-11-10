"""ID 生成收口：所有由中台生成的 ID 都从这里出。

    tenant_id / user_id 是调用方传入的；kb_id / document_id / chunk_id /
    retrieval_log_id / policy_id 由中台生成（UUID v4）；API Key 原文也在这里生成。

不要在各 service 里手写 uuid4()。将来要给 ID 加前缀、或换成雪花算法，
只改这个文件一处。

API Key 的格式（``rc_live_`` + 32 位随机十六进制）也收在这里：它出现在库里的
``api_keys.key_value``、文档、调试台与运维脚本里，散着写就会各自漂移。
"""

import secrets
import uuid
from typing import Final

#: Key 原文的固定前缀。``rk`` = retrieval key；``live`` 与将来的测试环境 Key 区分开。
API_KEY_PREFIX: Final[str] = "rc_live_"
#: 前缀之后的十六进制位数（``secrets.token_hex`` 每字节两位，所以要除以 2）。
API_KEY_HEX_LENGTH: Final[int] = 32
#: masked 形态保留末几位；4 位足够在同一面板里区分几把 Key，又不构成可用凭证。
API_KEY_MASK_TAIL: Final[int] = 4
#: masked 形态里的省略号。用单字符「…」而不是三个点：固定宽度截断时不会数错位数。
API_KEY_MASK_ELLIPSIS: Final[str] = "…"


def new_id() -> str:
    """生成一个 UUID v4 字符串。"""

    return str(uuid.uuid4())


def mask_api_key_value(key_value: str) -> str:
    """``rc_live_…9f2c``：保留前缀与末 4 位，中间截掉。

    规则与原文格式放在同一个文件里：面板摘要、日志与审计都用这一份，散着写就会各自漂移
    （一处露出 8 位、另一处露出 2 位，看起来都像「masked」）。短于前缀的异常值不在这里炸：
    它只可能是手写进库的坏数据，原样回显比 500 更可查。
    """

    if len(key_value) <= len(API_KEY_PREFIX) + API_KEY_MASK_TAIL:
        return key_value
    return f"{API_KEY_PREFIX}{API_KEY_MASK_ELLIPSIS}{key_value[-API_KEY_MASK_TAIL:]}"


def new_kb_id() -> str:
    return new_id()


def new_document_id() -> str:
    return new_id()


def new_chunk_id() -> str:
    return new_id()


def new_retrieval_log_id() -> str:
    return new_id()


def new_api_key_value() -> str:
    """生成 ``rc_live_`` + 32 位十六进制的 Key 原文（本期明文入库，见 ADR-0004）。

    用 :mod:`secrets` 而不是 :mod:`uuid`：Key 是凭证，必须取自操作系统的密码学
    随机源；UUID v4 有 122 位随机性但格式里含固定段，拼进前缀后容易被误当成
    「可预测的标识」处理。
    """

    return f"{API_KEY_PREFIX}{secrets.token_hex(API_KEY_HEX_LENGTH // 2)}"
