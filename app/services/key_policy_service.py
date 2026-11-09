"""改一套密钥配置（票 #72 收尾）：写之前跑与开租户同一份自洽校验。

为什么规则落在服务层而不是脚本里：控制面第 8 步要改的是同一件事，而「改配置前必须
自洽」这条规则只能有一处。脚本只是它的 CLI 适配器（读参数、打印、退码），与
``create_tenant`` / ``change_tenant_plan`` 的分层一致。

写顺序（与控制面两条写入口同一套收口）：

```text
读配置 → 读它所属的租户 → 读租户当前套餐 → 校验入参
       → 造「将来那一行」的候选快照 → 自洽校验（失败即返回，一行都不碰）
       → 落库
```

候选快照在**碰那一行之前**就造好并校验过，所以不自洽的请求连「改了一半」的库都留不下。
自洽校验是 :func:`app.services.profile_policy.ensure_policy_consistent`——与
:func:`app.services.tenant_service.create_tenant` 用的是同一个函数，脚本因此造不出
API 拒绝创建的那种配置。

**不需要清任何缓存。** 判定内核每个请求都重新
:func:`app.repositories.key_policy_repository.get_by_id` 一次配置行
（``app/core/auth.py::_identity_from_key``），配置没有缓存；套餐缓存只按 ``plan_id``
记套餐，与配置无关。所以改完的下一次调用就按新值判，Key 也不必重新签发。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Final, Mapping

from sqlalchemy.orm import Session

from app.models.key_policy import (
    PROFILES,
    KeyPolicy,
    KeyPolicySnapshot,
    policy_snapshot_of,
)
from app.models.plan import plan_spec_of
from app.repositories import key_policy_repository, plan_repository, tenant_repository
from app.services.profile_policy import ensure_policy_consistent

#: 能通过本函数改的**快照**字段：自定义档的手动参数与「不受限」标记刻意除外。
#: 后者是内置开发身份的显式标记，真实配置只有受限一种；前者由档位演进单独管理，
#: 不该在一条「改档位 / 改限额」的命令里被顺手覆盖。
POLICY_FIELDS: Final[tuple[str, ...]] = tuple(
    field.name
    for field in fields(KeyPolicySnapshot)
    if field.name not in ("custom_options", "unrestricted_scope")
)

#: 只落在行上、不在快照里的字段（快照不用它做判定，所以单独一行）。
ROW_FIELDS: Final[tuple[str, ...]] = ("name",)

#: 本函数接受的**全部**字段：未知字段直接报错，避免拼错一个列名却悄悄没改。
EDITABLE_FIELDS: Final[tuple[str, ...]] = POLICY_FIELDS + ROW_FIELDS

#: 必须 ≥ 1 的两个限额列：0 或负数不是一个合法额度，是「一次都不许用」的另一种写法。
NUMERIC_FIELDS: Final[tuple[str, ...]] = ("retrieve_qps", "retrieve_daily")


@dataclass(frozen=True)
class PolicyUpdate:
    """一次改配置的前后快照（同一形状，供脚本打印与将来写审计）。"""

    policy_id: str
    tenant_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    #: 这次**真的**变了的字段，按列名排序；空元组表示给出的字段都与原值相同。
    changed: tuple[str, ...]


def update_key_policy(
    db: Session, *, policy_id: str, changes: Mapping[str, Any]
) -> PolicyUpdate:
    """把 ``changes`` 里给出的字段改到这套配置上；失败时一行都不落库。

    错误分三类，与目录里的兄弟服务一致（脚本按这三类打消息、退 1）：

    - ``LookupError``：配置、它所属的租户、租户的套餐任一不存在；
    - ``ValueError``：入参本身不对（未知字段、未知档位、限额 ≤ 0、空配置名）；
    - ``AppError(20015)``：改完的配置**不自洽**（档位要的能力配置没开、配置开的比套餐
      允许的多、限额超过套餐上限）。它带着错误码，控制面第 8 步可以直接翻成响应。
    """

    policy = key_policy_repository.get_by_id(db, policy_id)
    if policy is None:
        raise LookupError(f"密钥配置不存在：{policy_id}")

    tenant = tenant_repository.get_by_id(db, policy.tenant_id)
    if tenant is None:
        raise LookupError(f"密钥配置所属租户不存在：{policy.tenant_id}")

    plan = plan_repository.get_by_id(db, tenant.plan_id)
    if plan is None:
        raise LookupError(f"租户 {tenant.id} 的套餐不存在：{tenant.plan_id}")

    values = _validated(changes)
    # 校验用的是**快照**（判定内核看到的那份形状），而不是还没 flush 的行：行上的
    # 布尔属性在写入前可能是 None，拿它做包含关系判断会得到「关着」的假结论。
    # ``name`` 只落在行上，不参与自洽校验，所以只把快照字段交给 ``replace``。
    snapshot_values = {column: values[column] for column in POLICY_FIELDS if column in values}
    candidate = replace(policy_snapshot_of(policy), **snapshot_values)
    ensure_policy_consistent(policy=candidate, plan=plan_spec_of(plan))

    before = _values_of(policy, policy_snapshot_of(policy))
    for column, value in snapshot_values.items():
        # ``allowed_kb_ids`` 在快照里是元组、在列上是 JSONB 数组；只在落库这一步换形状。
        setattr(policy, column, list(value) if column == "allowed_kb_ids" else value)
    if "name" in values:
        policy.name = values["name"]
    db.commit()

    after = _values_of(policy, policy_snapshot_of(policy))
    changed = tuple(sorted(column for column in values if before[column] != after[column]))
    return PolicyUpdate(
        policy_id=policy.id,
        tenant_id=policy.tenant_id,
        before=before,
        after=after,
        changed=changed,
    )


def _validated(changes: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规整入参，返回**只含快照字段**的值（``name`` 也在其中，单独处理）。

    全部校验在第一次写之前：一次改五项、最后一项非法时不能留下前四项已改的库。
    """

    if not changes:
        raise ValueError("没有要改的字段")

    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValueError(
            f"未知字段：{', '.join(unknown)}（可改：{', '.join(EDITABLE_FIELDS)}）"
        )

    values: dict[str, Any] = dict(changes)

    if "allowed_kb_ids" in values:
        kb_ids = tuple(values["allowed_kb_ids"])
        if any(not str(kb_id).strip() for kb_id in kb_ids):
            raise ValueError("白名单里的知识库标识不能为空")
        values["allowed_kb_ids"] = kb_ids

    if "profile" in values and values["profile"] not in PROFILES:
        raise ValueError(f"未知档位：{values['profile']}（可选：{'/'.join(PROFILES)}）")

    for column in NUMERIC_FIELDS:
        if column in values:
            limit = values[column]
            # ``bool`` 是 ``int`` 的子类：``--qps True`` 这类误用不能被当成 1 放行。
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError(f"{column} 必须是 ≥ 1 的整数，收到：{limit!r}")

    if "name" in values:
        name = str(values["name"]).strip()
        if not name:
            raise ValueError("配置名不能为空")
        values["name"] = name

    return values


def _values_of(policy: KeyPolicy, snapshot: KeyPolicySnapshot) -> dict[str, Any]:
    """行 + 快照 → 同一份「审查看得懂」的字段值：改前与改后都从这里造。

    左半边（``name``）只活在行上，右半边来自快照；两份拼成一个扁平字典，脚本才能
    打印「字段: 旧值 -> 新值」，将来审计也用同一个形状。
    """

    values: dict[str, Any] = {"name": policy.name}
    for field_name in POLICY_FIELDS:
        value = getattr(snapshot, field_name)
        values[field_name] = list(value) if field_name == "allowed_kb_ids" else value
    return values
