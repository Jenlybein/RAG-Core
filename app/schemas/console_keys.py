"""控制面 Key 与密钥配置（``/console/keys``、``/console/key-policies``）的请求 / 响应模型。

与数据面共用同一个响应壳，但**不复用它的入参**：数据面按 Key 鉴权、只收 ``kb_ids`` /
``query``，这一面的请求体是人手动填的「一把 Key + 一套配置」。

两处刻意的写法：

- **列表返回明文**（``key_value``）**和 mask** 两种形态。这是本票的明确决定（ADR-0004：
  调试台要拿原文打真实接口；由 ``GET /console/keys`` 的租户隔离兜住范围），masked 只是给
  列表摘要、日志和审计用的第二形态——只保留前缀与末 4 位，``rc_live_…9f2c``；
- **签发请求里的 ``policy`` 是一整组**，不是「可选字段 + 服务端补默认值」的拼接：一套配置
  必须一次说清「范围 / 五个授权位 / 三本账额度 / 三项参数 / 形状」，否则签发那一刻的自洽
  校验就没有输入。可选字段在这里显式给默认值，服务层看到的永远是**完整**的一组值。

**第 13 步起配置里没有档位名**（ADR-0032）：``profile`` 与 ``custom_options`` 已从表里
删除，用多好由 ``retrieval_mode`` / ``retrieval_top_k`` / ``retrieval_rerank`` 三项直参数
表达。它们写成具名字段（而不是一个自由字典）：值非法时要答「参数结构错误」（20002）而不是
先落进库里等运行时才发现。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.key_policy import KeyPolicy
from app.models.api_key import ApiKey
from app.utils.id_generator import mask_api_key_value


class KeyPolicyData(BaseModel):
    """一套密钥配置的**全量**值（列表、签发、改配置的响应都用这一个形状）。"""

    policy_id: str
    name: str
    # ── 第一组：数据范围 ──
    allowed_kb_ids: list[str]
    # ── 第二组：五个授权位 ──
    retrieve_allowed: bool
    rewrite_allowed: bool
    decompose_allowed: bool
    research_allowed: bool
    rerank_allowed: bool
    # ── 第三组：三本账额度（Key 级那一半）──
    retrieve_qps: int
    retrieve_daily: int
    rerank_qps: int
    rerank_daily: int
    llm_qps: int
    llm_daily: int
    # ── 第四组：三项直参数 ──
    retrieval_mode: str
    retrieval_top_k: int
    retrieval_rerank: bool
    # ── 第五组：结果形状 ──
    include_parent: bool
    expand_table_rows: bool


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
    """签发时随 Key 一起建的那套配置（全量）。

    默认值与 ``key_policies`` 列默认值**同源同义**：只开基线 ``retrieve`` 一个位、加工能力
    的两本账全 0、形状全开、白名单为空（空 = 什么都没圈定，不是「全部」）。QPS 与日配额
    没有默认值：它们必须由人填，「忘了填」不该被一个看起来合理的兜底值掩盖。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_kb_ids: list[str] = Field(default_factory=list)
    retrieve_allowed: bool = True
    rewrite_allowed: bool = False
    decompose_allowed: bool = False
    research_allowed: bool = False
    rerank_allowed: bool = False
    retrieve_qps: int
    retrieve_daily: int
    #: 加工能力的两本账**有默认值 0**，与列默认值同义：新 Key 不该自带一次改写或一次
    #: 约 30 秒的 Research。这也是它们与 ``retrieve_qps`` / ``retrieve_daily``（必须由人
    #: 填）的不同之处——「忘了填」在这里等于「不要」，而不是「额度未知」。
    rerank_qps: int = 0
    rerank_daily: int = 0
    llm_qps: int = 0
    llm_daily: int = 0
    retrieval_mode: str = "vector"
    retrieval_top_k: int = 5
    retrieval_rerank: bool = False
    include_parent: bool = True
    expand_table_rows: bool = True

    def provided(self) -> dict[str, Any]:
        """交给服务层的完整一组值；未知字段已在 schema 上被 ``extra=forbid`` 拦下。"""

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

    三项参数各自独立可改：面板上一次只动一个输入框，不该被迫把另外两项一起重发
    （重发就要处理「读到旧值再写回」的竞态）。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_kb_ids: list[str] | None = None
    retrieve_allowed: bool | None = None
    rewrite_allowed: bool | None = None
    decompose_allowed: bool | None = None
    research_allowed: bool | None = None
    rerank_allowed: bool | None = None
    retrieve_qps: int | None = None
    retrieve_daily: int | None = None
    rerank_qps: int | None = None
    rerank_daily: int | None = None
    llm_qps: int | None = None
    llm_daily: int | None = None
    retrieval_mode: str | None = None
    retrieval_top_k: int | None = None
    retrieval_rerank: bool | None = None
    include_parent: bool | None = None
    expand_table_rows: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)

    def provided(self) -> dict[str, Any]:
        """这次真的要改的字段：没发的、以及发了 ``null`` 的都不算。"""

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
        retrieve_allowed=policy.retrieve_allowed,
        rewrite_allowed=policy.rewrite_allowed,
        decompose_allowed=policy.decompose_allowed,
        research_allowed=policy.research_allowed,
        rerank_allowed=policy.rerank_allowed,
        retrieve_qps=policy.retrieve_qps,
        retrieve_daily=policy.retrieve_daily,
        rerank_qps=policy.rerank_qps,
        rerank_daily=policy.rerank_daily,
        llm_qps=policy.llm_qps,
        llm_daily=policy.llm_daily,
        retrieval_mode=policy.retrieval_mode,
        retrieval_top_k=policy.retrieval_top_k,
        retrieval_rerank=policy.retrieval_rerank,
        include_parent=policy.include_parent,
        expand_table_rows=policy.expand_table_rows,
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
