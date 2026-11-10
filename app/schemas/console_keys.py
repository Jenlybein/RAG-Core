"""控制面 Key 与密钥配置（``/console/keys``、``/console/key-policies``）的请求 / 响应模型。

与数据面共用同一个响应壳，但**不复用它的入参**：数据面按 Key 鉴权、只收 ``kb_ids`` /
``query``，这一面的请求体是人手动填的「一把 Key + 一套配置」。

两处刻意的写法：

- **列表返回明文**（``key_value``）**和 mask** 两种形态。这是本票的明确决定（ADR-0004：
  调试台要拿原文打真实接口；由 ``GET /console/keys`` 的租户隔离兜住范围），masked 只是给
  列表摘要、日志和审计用的第二形态——只保留前缀与末 4 位，``rc_live_…9f2c``；
- **签发请求里的 ``policy`` 是一整组**，不是「可选字段 + 服务端补默认值」的拼接：一套配置
  必须一次说清「范围 / 能力 / 限额 / 档位 / 形状」，否则签发那一刻的自洽校验就没有输入。
  可选字段在这里显式给默认值，服务层看到的永远是**完整**的 11 个值。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.key_policy import KeyPolicy
from app.models.api_key import ApiKey
from app.utils.id_generator import mask_api_key_value


class KeyPolicyData(BaseModel):
    """一套密钥配置的**全量**五组值（列表、签发、改配置的响应都用这一个形状）。"""

    policy_id: str
    name: str
    # ── 第一组：数据范围 ──
    allowed_kb_ids: list[str]
    # ── 第二组：能力开关 ──
    research_allowed: bool
    hybrid_allowed: bool
    rerank_allowed: bool
    query_rewrite_allowed: bool
    # ── 第三组：限额 ──
    retrieve_qps: int
    retrieve_daily: int
    # ── 第四组：档位 ──
    profile: str
    # ── 第五组：结果形状 ──
    include_parent: bool
    expand_table_rows: bool
    #: custom 档才读的手工参数。别的档位下这里的内容**不生效**（命名档位是完整替换）。
    custom_options: dict[str, Any]


class KeyData(BaseModel):
    """列表里的一行 / 签发成功后的那一行。

    ``policy`` 允许为空是**结构上的事实**：配置独立于 Key（删 Key 不删配置），反过来一条
    指向已不存在的配置的 Key 也可能被人手修库造出来。此时如实回 ``null``，而不是编一套
    默认值——那会让「这把 Key 能查什么」看起来还有答案。
    """

    key_id: str
    label: str | None
    key_value: str
    key_masked: str
    status: str
    policy: KeyPolicyData | None


class KeyListData(BaseModel):
    keys: list[KeyData]


class KeyPolicyInput(BaseModel):
    """签发时随 Key 一起建的那套配置（五组全量）。

    默认值与 ``key_policies`` 列默认值**同源同义**：能力全关、形状全开、白名单为空
    （空 = 什么都没圈定，不是「全部」）。档位、QPS、日配额没有默认值：它们必须由人填，
    「忘了填」不该被一个看起来合理的兜底值掩盖。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_kb_ids: list[str] = Field(default_factory=list)
    research_allowed: bool = False
    hybrid_allowed: bool = False
    rerank_allowed: bool = False
    query_rewrite_allowed: bool = False
    retrieve_qps: int
    retrieve_daily: int
    profile: str
    include_parent: bool = True
    expand_table_rows: bool = True
    custom_options: dict[str, Any] = Field(default_factory=dict)

    def provided(self) -> dict[str, Any]:
        """交给服务层的完整 11 个值；未知字段已在 schema 上被 ``extra=forbid`` 拦下。"""

        return self.model_dump()


class KeyIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 给人看的备注名（面板与日志用它分清「这是哪套集成」），鉴权永远不读它。
    label: str | None = Field(default=None, max_length=128)
    policy: KeyPolicyInput


class KeyDeleteData(BaseModel):
    key_id: str
    deleted: bool


class KeyPolicyPatchRequest(BaseModel):
    """改一套配置：**部分字段更新**，没发的（以及发 ``null`` 的）都表示「这次不动它」。

    ``custom_options`` 也在这条入口上，并且**不要求当前 ``profile`` 已经是 ``custom``**：
    它是延后生效的数据，面板可以先写好这批手动参数、下一次请求再切档位。若这里按当前
    档位拒绝，「切档位」与「写参数」就被迫压进同一个请求，而面板做不到原子。命名档位
    是完整替换、只有 ``custom`` 档才读这批键——这是 GLOSSARY「档位」那条的契约。

    ``custom_options`` 声明成 ``dict[str, Any]`` 而不是四个具名字段：键级校验放在服务层
    （:func:`app.services.key_policy_service._validated_custom_options`），失败才是业务错误
    ``20015``。写成具名字段会让 ``mode="bing"`` 这类值先被判成参数结构错误 ``20002``。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_kb_ids: list[str] | None = None
    research_allowed: bool | None = None
    hybrid_allowed: bool | None = None
    rerank_allowed: bool | None = None
    query_rewrite_allowed: bool | None = None
    retrieve_qps: int | None = None
    retrieve_daily: int | None = None
    profile: str | None = None
    include_parent: bool | None = None
    expand_table_rows: bool | None = None
    #: 键为 ``mode`` / ``top_k`` / ``rerank`` / ``rewrite``，允许只传其中几个；
    #: 部分更新是**合并**，不是整份替换。``null`` 表示「这次不动它」。
    custom_options: dict[str, Any] | None = None

    def provided(self) -> dict[str, Any]:
        """这次真的要写的字段：没发的、以及发了 ``null`` 的都不算。"""

        return self.model_dump(exclude_unset=True, exclude_none=True)


class KeyPolicyPatchData(BaseModel):
    policy_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    #: 这次**真的**变了的字段，按列名排序；空列表表示给出的字段都与原值相同。
    changed: list[str]


# ─────────────────────────────────────────────────────────── 行 → 响应体


def key_masked_of(key_value: str) -> str:
    """``rc_live_…9f2c``：保留前缀与末 4 位（规则本身收在 :mod:`app.utils.id_generator`）。"""

    return mask_api_key_value(key_value)


def policy_data_of(policy: KeyPolicy) -> KeyPolicyData:
    return KeyPolicyData(
        policy_id=policy.id,
        name=policy.name,
        allowed_kb_ids=list(policy.allowed_kb_ids),
        research_allowed=policy.research_allowed,
        hybrid_allowed=policy.hybrid_allowed,
        rerank_allowed=policy.rerank_allowed,
        query_rewrite_allowed=policy.query_rewrite_allowed,
        retrieve_qps=policy.retrieve_qps,
        retrieve_daily=policy.retrieve_daily,
        profile=policy.profile,
        include_parent=policy.include_parent,
        expand_table_rows=policy.expand_table_rows,
        custom_options=dict(policy.custom_options or {}),
    )


def key_data_of(api_key: ApiKey, policy: KeyPolicy | None) -> KeyData:
    return KeyData(
        key_id=api_key.id,
        label=api_key.label,
        key_value=api_key.key_value,
        key_masked=key_masked_of(api_key.key_value),
        status=api_key.status,
        policy=policy_data_of(policy) if policy is not None else None,
    )
