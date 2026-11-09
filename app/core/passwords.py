"""口令哈希：只用标准库（``hashlib.scrypt``），**不引入新依赖**。

项目把依赖钉到确切版本，仓库里没有任何口令哈希库。为一个函数加一个新依赖要连带锁文件、
镜像与供应链评审；而标准库的 ``scrypt`` 是同一类算法（内存硬，抗 GPU / ASIC 批量猜测），
参数还可以随部署调。

存储格式（一列字符串装下全部信息）::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

**参数必须跟着哈希一起存**：只存 ``scrypt$salt$hash`` 的话，「用哪组参数」就只剩代码里
那一份真相——今天调一次 n，昨天所有口令立刻验不过，而错误表现是「全员密码错误」。算法名
同理，将来换算法时老口令仍然能验，不必强制改密。

比对用 :func:`hmac.compare_digest`（常数时间）：``==`` 在第一个不同的字节就返回，
「猜对了前几个字节」于是变成可测量的信号。

**登录失败也要花掉一次哈希的时间**（:func:`dummy_verify`）：账号不存在就直接返回，
与「存在但密码错」差出一个 scrypt 的耗时，那点差异本身就是一个用户名枚举接口。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

#: 算法名，写进每一份哈希里。
ALGORITHM: str = "scrypt"
#: 盐长度：16 字节足够让「同一口令在不同行里哈希不同」。
SALT_BYTES: int = 16
#: 派生密钥长度。32 字节的碰撞空间远超任何口令的熵。
DERIVED_KEY_BYTES: int = 32
#: scrypt 参数。n 是 CPU/内存成本（2^14 → 约 16 MiB），r 是块大小，p 是并行度。
SCRYPT_N: int = 16384
SCRYPT_R: int = 8
SCRYPT_P: int = 1
#: OpenSSL 的默认内存上限是 32 MiB，而 128 * n * r = 16 MiB；显式给 64 MiB 留出余量，
#: 免得某台机器上因为默认值偏小而在登录时抛 ``ValueError: memory limit exceeded``。
MAX_MEMORY_BYTES: int = 64 * 1024 * 1024


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """把明文口令哈希成 ``scrypt$n$r$p$salt$hash``；``salt`` 只给测试固定用。

    空口令不在这里拦：它属于账号创建的入参校验（引导脚本 / 面板），而这里只回答
    「怎么把一个字符串变成一行可存、可验的哈希」。
    """

    actual_salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    digest = _derive(password, actual_salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            ALGORITHM,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _encode(actual_salt),
            _encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """按哈希里记着的参数重新派生并做常数时间比对；看不明白的哈希一律判失败。

    **不抛异常**：库里一行被手工改坏时，「这个口令不对」比一次 500 更接近事实，
    也不会把「哈希格式坏了」这件事泄露给调用方。
    """

    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    actual = _derive(password, salt, n, r, p)
    return hmac.compare_digest(actual, expected)


def dummy_verify(password: str) -> None:
    """走一遍与真校验等价的哈希计算，结果丢掉。

    账号不存在时**必须**调用它：否则「不存在」立刻返回、「密码错」要等一个 scrypt，
    两条路径的耗时差就是一个「这个用户名存在吗」的探针。
    """

    verify_password(password, _dummy_hash())


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """固定的一份哈希，口令取自随机源因此永远猜不中——它只用来消耗时间。

    用 ``lru_cache`` 而不是模块级常量：这份哈希只在有人登录失败时才需要算一次，
    让「导入这个模块」不必先付一次 scrypt 的代价。
    """

    return hash_password(secrets.token_urlsafe(32))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=DERIVED_KEY_BYTES,
        maxmem=MAX_MEMORY_BYTES,
    )


def _encode(raw: bytes) -> str:
    """URL 安全的 base64（去掉填充）：盐与哈希进的是数据库列，不留 ``+/=`` 这类需要转义的字符。"""

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """拆哈希串；格式不对、算法不认识、参数不是正整数都返回 ``None``。"""

    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != ALGORITHM:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _decode(parts[4])
        expected = _decode(parts[5])
    except (ValueError, TypeError):
        return None
    if min(n, r, p) < 1 or not salt or not expected:
        return None
    return n, r, p, salt, expected
