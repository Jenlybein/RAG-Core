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
from app.utils.id_generator import new_id

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

#: 签发时新建配置接受的字段：改配置能改的十个 + ``custom_options``（只在新建时确定）。
CREATE_FIELDS: Final[tuple[str, ...]] = POLICY_FIELDS + ("custom_options",)

#: 新建配置时**必须**由调用方给出的三个值：档位、QPS、日配额。忘了填不该被一个看起来
#: 合理的兜底值掩盖——「默认 10 QPS」会悄悄发出比调用方预期更大的额度。
CREATE_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "profile",
    "retrieve_qps",
    "retrieve_daily",
)

#: 新建配置时可省略字段的默认值：与 ``key_policies`` 列默认值同义（能力全关、形状全开、
#: 白名单为空 = 什么都还没圈定）。显式写出来，不依赖列默认值——列默认值哪天被改成 ``True``，
#: 新建的 Key 就自带重排与改写，而签发路径不会有任何异常。
CREATE_DEFAULTS: Final[dict[str, Any]] = {
    "allowed_kb_ids": (),
    "research_allowed": False,
    "hybrid_allowed": False,
    "rerank_allowed": False,
    "query_rewrite_allowed": False,
    "include_parent": True,
    "expand_table_rows": True,
    "custom_options": {},
}

#: 必须 ≥ 1 的两个限额列：0 或负数不是一个合法额度，是「一次都不许用」的另一种写法。
NUMERIC_FIELDS: Final[tuple[str, ...]] = ("retrieve_qps", "retrieve_daily")


@dataclass(frozen=True)
class CreatedPolicy:
    """一次新建配置的产物：配置行 + 它落库后的全量值（审计的 after 侧直接用这份）。"""

    policy: KeyPolicy
    values: dict[str, Any]


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

    result = apply_key_policy_update(db, policy_id=policy_id, changes=changes)
    db.commit()
    return result


def apply_key_policy_update(
    db: Session, *, policy_id: str, changes: Mapping[str, Any]
) -> PolicyUpdate:
    """与 :func:`update_key_policy` 同一套校验与写入，但**不提交**。

    控制面（票 #91）要把这次改动与它的一条审计放进同一个事务：先提交再写账本会留下
    「改了但没有审计」的窗口，而审计落库失败时整次改动应当一起回滚。校验在第一次写之前
    完成，所以不提交的版本同样不会留下「改了一半」的库。
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

    before = policy_values_of(policy)
    for column, value in snapshot_values.items():
        # ``allowed_kb_ids`` 在快照里是元组、在列上是 JSONB 数组；只在落库这一步换形状。
        setattr(policy, column, list(value) if column == "allowed_kb_ids" else value)
    if "name" in values:
        policy.name = values["name"]

    after = policy_values_of(policy)
    changed = tuple(sorted(column for column in values if before[column] != after[column]))
    return PolicyUpdate(
        policy_id=policy.id,
        tenant_id=policy.tenant_id,
        before=before,
        after=after,
        changed=changed,
    )


def create_key_policy(
    db: Session, *, tenant_id: str, name: str, values: Mapping[str, Any]
) -> CreatedPolicy:
    """给租户新建一套密钥配置；失败时一行都不落库。

    与 :func:`update_key_policy` 共用同一份自洽校验（:func:`ensure_policy_consistent`），
    所以控制面签发出来的配置与 ``create_tenant`` 开租户时建的那套遵守**完全相同的**
    「档位 ⊆ 配置 ⊆ 套餐」与数值上限。校验判在落库之前：不自洽的配置一旦进库，第一次调用
    就撞门禁，而那时改的是客户的生产配置。

    错误形状与兄弟服务一致：``LookupError``（租户 / 套餐不存在）、``ValueError``（入参
    不对）、``AppError(20015)``（配置不自洽）。**不提交**：签发 Key 时这条配置、那把 Key
    与一条审计必须同一个事务。
    """

    policy_name = str(name).strip()
    if not policy_name:
        raise ValueError("配置名不能为空")

    tenant = tenant_repository.get_by_id(db, tenant_id)
    if tenant is None:
        raise LookupError(f"租户不存在：{tenant_id}")
    plan = plan_repository.get_by_id(db, tenant.plan_id)
    if plan is None:
        raise LookupError(f"租户 {tenant_id} 的套餐不存在：{tenant.plan_id}")

    checked = _validated_create(values)
    row = KeyPolicy(
        id=new_id(),
        tenant_id=tenant_id,
        name=policy_name,
        allowed_kb_ids=list(checked["allowed_kb_ids"]),
        research_allowed=checked["research_allowed"],
        hybrid_allowed=checked["hybrid_allowed"],
        rerank_allowed=checked["rerank_allowed"],
        query_rewrite_allowed=checked["query_rewrite_allowed"],
        retrieve_qps=checked["retrieve_qps"],
        retrieve_daily=checked["retrieve_daily"],
        profile=checked["profile"],
        include_parent=checked["include_parent"],
        expand_table_rows=checked["expand_table_rows"],
        custom_options=checked["custom_options"],
    )
    ensure_policy_consistent(policy=policy_snapshot_of(row), plan=plan_spec_of(plan))

    key_policy_repository.create(db, row)
    return CreatedPolicy(policy=row, values=policy_values_of(row))


def policy_values_of(policy: KeyPolicy) -> dict[str, Any]:
    """一套配置的行级全量值：审计的 before / after 侧都从这里造。"""

    return _values_of(policy, policy_snapshot_of(policy))


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


def _validated_create(values: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规整「新建配置」的入参，返回可直接落到行上的完整 11 个值。

    与 :func:`_validated` 共用同一套逐字段校验（档位名、限额 ≥ 1、白名单非空串），只是
    新建时字段可以省略、由 :data:`CREATE_DEFAULTS` 补齐；``custom_options`` 是新建路径
    独有的一个（改配置那条路刻意不动它）。
    """

    provided = dict(values)
    unknown = sorted(set(provided) - set(CREATE_FIELDS))
    if unknown:
        raise ValueError(
            f"未知字段：{', '.join(unknown)}（可选：{', '.join(CREATE_FIELDS)}）"
        )

    missing = [column for column in CREATE_REQUIRED_FIELDS if column not in provided]
    if missing:
        raise ValueError(f"缺少必填字段：{', '.join(missing)}")

    merged = {**CREATE_DEFAULTS, **provided}
    custom_options = merged.pop("custom_options", None) or {}
    if not isinstance(custom_options, dict):
        raise ValueError(f"custom_options 必须是对象，收到：{type(custom_options).__name__}")

    checked = _validated(merged)
    checked["custom_options"] = dict(custom_options)
    return checked


def _values_of(policy: KeyPolicy, snapshot: KeyPolicySnapshot) -> dict[str, Any]:
    """行 + 快照 → 同一份「审查看得懂」的字段值：改前与改后都从这里造。

    左半边（``name``）只活在行上，右半边来自快照；两份拼成一个扁平字典，脚本才能
    打印「字段: 旧值 -> 新值」，审计也用同一个形状。``custom_options`` 一并带上：新建时
    它是配置内容的一部分，审计里缺了它就看不出「这套 custom 参数是什么」。
    """

    values: dict[str, Any] = {"name": policy.name}
    for field_name in POLICY_FIELDS:
        value = getattr(snapshot, field_name)
        values[field_name] = list(value) if field_name == "allowed_kb_ids" else value
    values["custom_options"] = dict(snapshot.custom_options)
    return values
