"""表 8：key_policies —— 密钥配置（签发 API Key 时绑定的那一套）。

一套配置回答「**这套集成**能用多好、能用多少」：数据范围（可用知识库白名单）、能力
开关、限额、默认档位、结果形状偏好。它属于租户、租户内可复用，所以独立成表——若把
这些字段直接摊进 ``api_keys``，租户内换一把 Key 就得重建一遍配置，改一处忘一处，
而「删掉配置 → 引用它的 Key 一起失效」也没地方写。本期按一对一用，但表结构不锁死这一点。

``profile``（档位）与能力开关是两件事：命名档位是**完整替换**（``quality`` 承诺
混合 + 重排 + 改写整条链路），配置里的开关是租户在上限内给这套集成的额外分配。
自洽校验（档位所需能力 ⊆ 配置开关 ⊆ 套餐能力）不在本表实现，由服务层在写入前判——
列上能做的只有「别让空值冒出来」，做了也表达不出三层包含关系。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now

# 四个质量档位名。写成常量而不是散落的字面量：套餐的 ``allowed_profiles`` 与
# 密钥配置的 ``profile`` 必须用同一套词，两边各写一遍迟早拼错一个。
PROFILE_SPEED: Final[str] = "speed"
PROFILE_BALANCED: Final[str] = "balanced"
PROFILE_QUALITY: Final[str] = "quality"
PROFILE_CUSTOM: Final[str] = "custom"
PROFILES: Final[tuple[str, ...]] = (
    PROFILE_SPEED,
    PROFILE_BALANCED,
    PROFILE_QUALITY,
    PROFILE_CUSTOM,
)


class KeyPolicy(Base):
    __tablename__ = "key_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 裸字符串、不加外键：与既有四张业务表的风格一致，引用完整性由应用层校验。
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 数据范围：这套集成能查哪些库。空列表表示「还没圈定」，不是「全部」——
    # 「全部」一旦成为空列表的含义，把白名单清空就成了开闸。
    allowed_kb_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 能力开关：租户在套餐上限内给这套集成的分配。默认全关——新发一把 Key 不该自带
    # 重排与改写，那是最贵的两项。
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    hybrid_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    query_rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 限额：Key 级计数卡住单个外部应用，租户级聚合由套餐上限卡住（两级计数）。
    retrieve_qps: Mapped[int] = mapped_column(Integer)
    retrieve_daily: Mapped[int] = mapped_column(Integer)
    # 建 Key 时选定的档位；调用方改不了。改档位就是改这张配置，改完立刻生效。
    profile: Mapped[str] = mapped_column(String(32))
    # 结果形状偏好：折叠成整节 / 表格行折回整表。默认都开（与既有响应默认一致），
    # 否则同一把 Key 升级前后返回形状会变。
    include_parent: Mapped[bool] = mapped_column(Boolean, default=True)
    expand_table_rows: Mapped[bool] = mapped_column(Boolean, default=True)
    # custom 档才读的手工参数（检索方式、返回条数、是否重排、是否改写）。存 JSONB 是
    # 因为它的键随档位演进而变，逐个开列会让「custom 档多一个参数」变成一次迁移；
    # 但它的**语义**只在 profile='custom' 时成立，别的档位下这里的内容不生效。
    custom_options: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


@dataclass(frozen=True)
class KeyPolicySnapshot:
    """一套密钥配置的**只读快照**：判定内核把它随身带着，下游只读不写。

    与 :class:`app.models.plan.PlanSpec` 同一套做法：字段一一对应行里的列、没有默认值
    （映射漏一列就是 ``TypeError``），且**不做 ``bool()`` / ``int()`` 兜底**——行里某列
    是 ``NULL`` 时兜底会把它变成 ``False`` 或 ``0``，而「能力全关」恰好是合法组合，
    于是一个播种 / 落库错误会静静通过。

    为什么单独做一个快照而不是直接拿着 ORM 行：行绑在会话生命周期上，请求结束就过期；
    而判定结论要被后面的关卡（白名单、套餐复核、限流、配额）在同一个请求里反复读，
    读到一个已经 detach 的行只会得到 ``DetachedInstanceError``，比「读到旧值」更难查。

    ``custom_options`` 用「（键, 值）对」而不是 ``dict``：frozen 的 dataclass 里塞一个
    可变 dict，等于给「冻结」留了个后门——拿到快照的人可以顺手改掉它，而这份快照可能
    正被同一个请求里的其它关卡共享。要字典形状时调用方自己 ``dict(...)`` 造一份。
    """

    allowed_kb_ids: tuple[str, ...]
    research_allowed: bool
    hybrid_allowed: bool
    rerank_allowed: bool
    query_rewrite_allowed: bool
    retrieve_qps: int
    retrieve_daily: int
    profile: str
    include_parent: bool
    expand_table_rows: bool
    custom_options: tuple[tuple[str, Any], ...] = ()


def policy_snapshot_of(row: KeyPolicy) -> KeyPolicySnapshot:
    """把一行密钥配置映射成快照；逐字段搬运，不做任何类型兜底（理由见上）。"""

    return KeyPolicySnapshot(
        allowed_kb_ids=tuple(row.allowed_kb_ids),
        research_allowed=row.research_allowed,
        hybrid_allowed=row.hybrid_allowed,
        rerank_allowed=row.rerank_allowed,
        query_rewrite_allowed=row.query_rewrite_allowed,
        retrieve_qps=row.retrieve_qps,
        retrieve_daily=row.retrieve_daily,
        profile=row.profile,
        include_parent=row.include_parent,
        expand_table_rows=row.expand_table_rows,
        # 按键排序：JSONB 本来就重排键，这里再稳定一次，快照相等性才不依赖读出顺序。
        custom_options=tuple(sorted((row.custom_options or {}).items())),
    )
