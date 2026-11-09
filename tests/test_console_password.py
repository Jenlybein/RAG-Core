"""票 #80：口令哈希（只用标准库）。

这一票不新增依赖，所以口令哈希是 ``hashlib.scrypt`` 手写的一层。要守四件事：

1. **存的不是明文**，且同一个口令两次哈希不同（逐口令的盐）；
2. **算法与参数随哈希一起存**：调一次 n 不会让昨天所有口令验不过；
3. 比对是常数时间（``hmac.compare_digest``），坏掉的哈希判失败而不抛异常；
4. **账号不存在时也花一次哈希的时间**——否则「查无此人」与「密码不对」的耗时差
   就是一个用户名枚举接口。
"""

from __future__ import annotations

import hashlib

import pytest

from app.core import passwords


def test_a_hash_does_not_contain_the_password_and_carries_its_own_parameters() -> None:
    encoded = passwords.hash_password("correct horse battery staple")

    assert "correct horse battery staple" not in encoded
    algorithm, n, r, p, salt, digest = encoded.split("$")
    assert algorithm == "scrypt"
    assert (int(n), int(r), int(p)) == (passwords.SCRYPT_N, passwords.SCRYPT_R, passwords.SCRYPT_P)
    assert salt and digest


def test_the_same_password_hashes_differently_every_time() -> None:
    """逐口令的盐：两行同口令的哈希相等，就等于「一眼看出谁和谁口令一样」。"""

    assert passwords.hash_password("same-password") != passwords.hash_password("same-password")


def test_verify_accepts_the_right_password_and_rejects_anything_else() -> None:
    encoded = passwords.hash_password("s3cret")

    assert passwords.verify_password("s3cret", encoded) is True
    assert passwords.verify_password("S3cret", encoded) is False
    assert passwords.verify_password("", encoded) is False


def test_a_broken_hash_fails_closed_instead_of_raising() -> None:
    """库里一行被手工改坏时，「口令不对」比一次 500 更接近事实。"""

    for broken in ("", "plaintext", "scrypt$1$2$3", "bcrypt$16384$8$1$aa$bb", "scrypt$x$8$1$aa$bb"):
        assert passwords.verify_password("whatever", broken) is False


def test_parameters_travel_with_the_hash_so_old_rows_keep_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """老行（旧参数）在新代码上仍然验得过：参数在字符串里，不在代码里。"""

    encoded = passwords.hash_password("legacy")

    monkeypatch.setattr(passwords, "SCRYPT_N", 2**12)
    assert passwords.verify_password("legacy", encoded) is True


def test_dummy_verify_costs_a_real_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    """账号不存在时也要跑一次 scrypt——耗时相同才不会泄露「这个用户名存在吗」。"""

    calls: list[bytes] = []
    real_scrypt = hashlib.scrypt

    def _counting(*args: object, **kwargs: object) -> bytes:
        calls.append(b"called")
        return real_scrypt(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(passwords.hashlib, "scrypt", _counting)

    passwords.dummy_verify("anything-at-all")

    assert calls, "失败路径必须真的付一次哈希的代价"
