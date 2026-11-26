"""表 8：key_policies —— 密钥配置（签发 API Key 时绑定的那一套）。

一套配置回答「**这套集成**能用多好、能用多少」：数据范围（可用知识库白名单）、五个
授权位、三本账额度（Key 级那一半）、三项直参数与结果形状偏好。它属于租户、租户内可
复用，所以独立成表——若把这些字段直接摊进 ``api_keys``，租户内换一把 Key 就得重建
一遍配置，改一处忘一处，而「删掉配置 → 引用它的 Key 一起失效」也没地方写。

**第 13 步起后端没有「档位」这个概念**：一套配置用多好由三项直参数
``retrieval_mode / retrieval_top_k / retrieval_rerank`` 表达，档位名（speed /
balanced / quality）只剩前端填充按钮与文档别名两处身份，不进表、不进快照、不参与
任何判定。授权位回答「能不能用这条端点」，额度回答「用了多少」，参数回答「用多好」，
三者是三种事实，各存各的列（ADR-0032）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class KeyPolicy(Base):
    __tablename__ = "key_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 裸字符串、不加外键：与既有四张业务表的风格一致，引用完整性由应用层校验。
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 数据范围：这套集成能查哪些库。空列表表示「还没圈定」，不是「全部」——
    # 「全部」一旦成为空列表的含义，把白名单清空就成了开闸。
    allowed_kb_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # ── 五个授权位（能不能用）：与 ``app.services.retrieval_authorization.CAPABILITIES``
    # 一一对应，列名 = 能力名 + ``_allowed``。默认只开基线 ``retrieve``——
    # 新发一把 Key 不该自带加工能力与重排，那是最贵的几项。
    retrieve_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    decompose_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── 三本账的 Key 级那一半（用多少）：retrieve 记内核调用次数、rerank 记重排次数、
    # llm 记实际发生的大模型调用次数。两级计数：本表卡单个外部应用，套餐卡租户聚合。
    retrieve_qps: Mapped[int] = mapped_column(Integer)
    retrieve_daily: Mapped[int] = mapped_column(Integer)
    rerank_qps: Mapped[int] = mapped_column(Integer, default=0)
    rerank_daily: Mapped[int] = mapped_column(Integer, default=0)
    llm_qps: Mapped[int] = mapped_column(Integer, default=0)
    llm_daily: Mapped[int] = mapped_column(Integer, default=0)
    # ── 三项直参数（用多好）：档位退役后这就是唯一真相。
    retrieval_mode: Mapped[str] = mapped_column(String(16), default="vector")
    retrieval_top_k: Mapped[int] = mapped_column(Integer, default=5)
    retrieval_rerank: Mapped[bool] = mapped_column(Boolean, default=False)
    # 结果形状偏好：折叠成整节 / 表格行折回整表。默认都开（与既有响应默认一致）。
    include_parent: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 一条结果 `content` 的硬上限（字符，第 16 步 #207）。与库的节段阈值同值 2000：
    #: 正常情况下节段 ≤ 阈值 ≤ 上限，永不触发；它管的是老索引、表格与「片边界超阈值」。
    max_unit_chars: Mapped[int] = mapped_column(Integer, default=2000)
    expand_table_rows: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


@dataclass(frozen=True)
class KeyPolicySnapshot:
    """一套密钥配置的**只读快照**：判定内核把它随身带着，下游只读不写。

    与 :class:`app.models.plan.PlanSpec` 同一套做法：字段一一对应行里的列、**没有默认值**
    （映射漏一列就是 ``TypeError``），且**不做 ``bool()`` / ``int()`` 兜底**——行里某列
    是 ``NULL`` 时兜底会把它变成 ``False`` 或 ``0``，而「能力全关」恰好是合法组合，
    于是一个播种 / 落库错误会静静通过。

    为什么单独做一个快照而不是直接拿着 ORM 行：行绑在会话生命周期上，请求结束就过期；
    而判定结论要被后面的关卡（白名单、套餐复核、限流、配额）在同一个请求里反复读，
    读到一个已经 detach 的行只会得到 ``DetachedInstanceError``，比「读到旧值」更难查。

    ``unrestricted_scope`` 是「这把 Key 不做数据范围收窄」的**显式标记**，默认
    ``False``（受限）。它存在是因为内置开发身份没有白名单可言，而 ``allowed_kb_ids``
    的空元组在仓库里的含义是「什么都没圈定、所以什么都不许」——让同一个空值兼职表达
    「全部」会让「清空白名单」变成开闸。真实配置经 :func:`policy_snapshot_of` 映射
    出来永远是受限的；只有合成的开发快照把它置为 ``True``。
    """

    allowed_kb_ids: tuple[str, ...]
    retrieve_allowed: bool
    rewrite_allowed: bool
    decompose_allowed: bool
    research_allowed: bool
    rerank_allowed: bool
    retrieve_qps: int
    retrieve_daily: int
    rerank_qps: int
    rerank_daily: int
    llm_qps: int
    llm_daily: int
    retrieval_mode: str
    retrieval_top_k: int
    retrieval_rerank: bool
    include_parent: bool
    expand_table_rows: bool
    #: 一条结果 ``content`` 的硬上限（字符数，第 16 步 #207）。
    max_unit_chars: int
    #: 只有内置开发身份的那份快照把它打开（见类文档末段）；缺省 = 受限。
    unrestricted_scope: bool = False


def policy_snapshot_of(row: KeyPolicy) -> KeyPolicySnapshot:
    """把一行密钥配置映射成快照；逐字段搬运，不做任何类型兜底（理由见上）。"""

    return KeyPolicySnapshot(
        allowed_kb_ids=tuple(row.allowed_kb_ids),
        retrieve_allowed=row.retrieve_allowed,
        rewrite_allowed=row.rewrite_allowed,
        decompose_allowed=row.decompose_allowed,
        research_allowed=row.research_allowed,
        rerank_allowed=row.rerank_allowed,
        retrieve_qps=row.retrieve_qps,
        retrieve_daily=row.retrieve_daily,
        rerank_qps=row.rerank_qps,
        rerank_daily=row.rerank_daily,
        llm_qps=row.llm_qps,
        llm_daily=row.llm_daily,
        retrieval_mode=row.retrieval_mode,
        retrieval_top_k=row.retrieval_top_k,
        retrieval_rerank=row.retrieval_rerank,
        include_parent=row.include_parent,
        max_unit_chars=row.max_unit_chars,
        expand_table_rows=row.expand_table_rows,
    )
