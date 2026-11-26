"""测试用的 Key 铸造：一行 :class:`ApiKey` + 它的原文。

库里只存哈希之后，「造一把能通过鉴权的 Key」需要两样东西同时成立：行里是
``key_hash`` / ``key_tail``，请求头里是原文。这两样由不同的地方提供，而它们必须互相
一致——所以只在这里算一次，五个测试文件都调它。

``plaintext`` 可显式给（用例要断言末 5 位、要写固定的 Bearer 值时），但它必须属于同一个
``key_id``：串里的行身份证与行主键对不上，鉴权会走成「Key 不存在」，而症状看起来像是
哈希坏了。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.passwords import hash_password
from app.models.api_key import ApiKey
from app.utils.id_generator import (
    api_key_tail_of,
    new_api_key_value,
    split_api_key_value,
)


@dataclass(frozen=True)
class TestKey:
    """一行 Key 与它的原文；``authorization`` 直接丢进请求头。"""

    api_key: ApiKey
    plaintext: str

    @property
    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.plaintext}"}

    @property
    def secret(self) -> str:
        parsed = split_api_key_value(self.plaintext)
        assert parsed is not None, f"测试 Key 形状不对：{self.plaintext!r}"
        return parsed[1]


def mint_key(
    *,
    key_id: str,
    tenant_id: str,
    policy_id: str,
    label: str | None = None,
    status: str = "active",
    plaintext: str | None = None,
) -> TestKey:
    """造一把 Key：``key_id`` 定主键，其余字段落到行上，哈希由原文算出。"""

    value = plaintext if plaintext is not None else new_api_key_value(key_id)
    parsed = split_api_key_value(value)
    assert parsed is not None, f"测试 Key 形状不对：{value!r}"
    value_key_id, secret = parsed
    assert value_key_id == key_id, (
        f"原文里的行身份证 {value_key_id!r} 与行主键 {key_id!r} 不一致：鉴权会走成「Key 不存在」"
    )

    api_key = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        policy_id=policy_id,
        key_hash=hash_password(secret),
        key_tail=api_key_tail_of(secret),
        label=label,
        status=status,
    )
    return TestKey(api_key=api_key, plaintext=value)
