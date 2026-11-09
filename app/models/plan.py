"""表 6：plans —— 套餐（free / standard / pro）。

**这张表才是真相，本文件里的三档常量只是首次播种的种子。** 上限与能力开关是面板上
会出现输入框的东西，全部落在库里；若某列只活在代码里，运营想给一个客户临时提额度就
只能改代码发版，改错了也没有预览与审计——「界面上改得动、后端读不到」比「界面上没有
这个字段」更糟。

11 个业务列（:data:`PLAN_BUSINESS_COLUMNS`）= 档位白名单 1 + 能力开关 4 + 上限 6。
逐列对账由 :func:`preset_mismatches` 负责：播种之后把行映射回上下文，与种子逐字段
比对，**少一列就是可见的失败**。

``plan_id`` 是整数自增主键，别的表只存这个数字（不加外键，见票面「裸字符串 id、不加
外键」的既有风格）；``code`` 唯一，是给人看与给脚本用的名字。想改用 UUID 或把三档塞进
一张字典表都会破坏「一档一列、面板一字段一列」的对应关系。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now
from app.models.key_policy import (
    PROFILE_BALANCED,
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
)

PLAN_FREE: Final[str] = "free"
PLAN_STANDARD: Final[str] = "standard"
PLAN_PRO: Final[str] = "pro"

# 三档的名字就是 ``code``：脚本入参、库里的行、错误信息里出现的都是这三个词之一。
PLAN_CODES: Final[tuple[str, ...]] = (PLAN_FREE, PLAN_STANDARD, PLAN_PRO)

#: 面板上会出现输入框的 11 列；:func:`preset_mismatches` 按这个元组逐列比对。
#: 顺序只影响对账报告的可读性，不参与任何判定。
PLAN_BUSINESS_COLUMNS: Final[tuple[str, ...]] = (
    "allowed_profiles",
    "hybrid_allowed",
    "rerank_allowed",
    "query_rewrite_allowed",
    "research_allowed",
    "retrieve_qps",
    "retrieve_daily",
    "max_kb",
    "max_kb_per_retrieve",
    "max_documents_per_kb",
    "max_processing_documents",
)


class Plan(Base):
    __tablename__ = "plans"

    # 整数自增主键：引用方（tenants / 将来别的表）只需要存一个数字。用 uuid 表达
    # 「三档」只是让每行多 32 个字符，还得额外维护 code 唯一。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 档位白名单：这个档位允许用哪些质量档位（speed / balanced / quality / custom）。
    # 存成白名单而不是「最低档位」：将来出现新的档位组合时，白名单能表达「只给这两档」，
    # 序关系表达不了。
    allowed_profiles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 四个能力开关与六个上限：拆成独立列而不是一个大 JSONB，是因为面板要逐字段渲染、
    # 校验、写审计；塞进 JSONB 之后，「这次改了哪一项」就又变成要解析的内容了。
    hybrid_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    query_rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    retrieve_qps: Mapped[int] = mapped_column(Integer)
    retrieve_daily: Mapped[int] = mapped_column(Integer)
    max_kb: Mapped[int] = mapped_column(Integer)
    max_kb_per_retrieve: Mapped[int] = mapped_column(Integer)
    max_documents_per_kb: Mapped[int] = mapped_column(Integer)
    max_processing_documents: Mapped[int] = mapped_column(Integer)
    # 只有 updated_at，没有 created_at：套餐是「系统级配置」，被关心的是「上一次谁把它
    # 改成了什么」，第一版什么时候建出来的没有运营含义。
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


@dataclass(frozen=True)
class PlanSpec:
    """一档套餐的完整快照：既当种子常量用，也当「行 → 上下文」的映射结果。

    字段与列一一对应，且**没有默认值**：映射代码漏读一列就是 ``TypeError``，
    不可能悄悄变成 ``None`` 或某个「看起来合理」的兜底值。
    """

    code: str
    name: str
    allowed_profiles: tuple[str, ...]
    hybrid_allowed: bool
    rerank_allowed: bool
    query_rewrite_allowed: bool
    research_allowed: bool
    retrieve_qps: int
    retrieve_daily: int
    max_kb: int
    max_kb_per_retrieve: int
    max_documents_per_kb: int
    max_processing_documents: int


#: 三档的种子值。**只在 plans 表为空时写入**（见 ``identity_seeding.seed_plans``），
#: 之后库里的行才是真相——运营改过的额度不允许被下一次启动悄悄改回去。
PLAN_PRESETS: Final[dict[str, PlanSpec]] = {
    PLAN_FREE: PlanSpec(
        code=PLAN_FREE,
        name="免费版",
        allowed_profiles=(PROFILE_SPEED,),
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        research_allowed=False,
        retrieve_qps=3,
        retrieve_daily=500,
        max_kb=1,
        max_kb_per_retrieve=1,
        max_documents_per_kb=30,
        max_processing_documents=1,
    ),
    PLAN_STANDARD: PlanSpec(
        code=PLAN_STANDARD,
        name="标准版",
        allowed_profiles=(PROFILE_SPEED, PROFILE_BALANCED, PROFILE_CUSTOM),
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        research_allowed=False,
        retrieve_qps=10,
        retrieve_daily=5000,
        max_kb=5,
        max_kb_per_retrieve=3,
        max_documents_per_kb=200,
        max_processing_documents=2,
    ),
    PLAN_PRO: PlanSpec(
        code=PLAN_PRO,
        name="专业版",
        allowed_profiles=(PROFILE_SPEED, PROFILE_BALANCED, PROFILE_QUALITY, PROFILE_CUSTOM),
        hybrid_allowed=True,
        rerank_allowed=True,
        query_rewrite_allowed=True,
        research_allowed=True,
        retrieve_qps=50,
        retrieve_daily=100000,
        max_kb=50,
        max_kb_per_retrieve=5,
        max_documents_per_kb=5000,
        max_processing_documents=10,
    ),
}


def check_plan_code(code: str) -> str:
    """校验档位名；不认识就立刻失败，并把已知的三档列出来。

    不提供「猜一个最接近的」或「回落到最低档」：改套餐时把 ``pro`` 拼错成 ``Pro``
    若被静默降级成免费档，客户会先掉额度再收到投诉，而日志里什么异常都没有。
    """

    if code not in PLAN_CODES:
        raise ValueError(f"未知套餐档位：{code}（可选：{'/'.join(PLAN_CODES)}）")
    return code


def plan_spec_of(row: Plan) -> PlanSpec:
    """把一行套餐映射成上下文快照。

    **不做 ``bool()`` / ``int()`` 之类的类型兜底**：行里某列是 ``NULL`` 时，兜底会把
    它变成 ``False`` 或 ``0``，而最低档的四个开关恰好都是 ``False``——于是一个「播种时
    漏了这一列」的错误会在对账里静默通过。原样取值，``None`` 才会与常量比出差异。
    """

    return PlanSpec(
        code=row.code,
        name=row.name,
        allowed_profiles=tuple(row.allowed_profiles),
        hybrid_allowed=row.hybrid_allowed,
        rerank_allowed=row.rerank_allowed,
        query_rewrite_allowed=row.query_rewrite_allowed,
        research_allowed=row.research_allowed,
        retrieve_qps=row.retrieve_qps,
        retrieve_daily=row.retrieve_daily,
        max_kb=row.max_kb,
        max_kb_per_retrieve=row.max_kb_per_retrieve,
        max_documents_per_kb=row.max_documents_per_kb,
        max_processing_documents=row.max_processing_documents,
    )


def plan_row_of(spec: PlanSpec) -> Plan:
    """种子 → 待插入的行。只给「表为空时的首次播种」用，别拿它覆盖已有行。"""

    return Plan(
        code=spec.code,
        name=spec.name,
        allowed_profiles=list(spec.allowed_profiles),
        hybrid_allowed=spec.hybrid_allowed,
        rerank_allowed=spec.rerank_allowed,
        query_rewrite_allowed=spec.query_rewrite_allowed,
        research_allowed=spec.research_allowed,
        retrieve_qps=spec.retrieve_qps,
        retrieve_daily=spec.retrieve_daily,
        max_kb=spec.max_kb,
        max_kb_per_retrieve=spec.max_kb_per_retrieve,
        max_documents_per_kb=spec.max_documents_per_kb,
        max_processing_documents=spec.max_processing_documents,
    )


def preset_mismatches(rows: Iterable[Plan]) -> list[str]:
    """逐列对账：返回差异清单（``档位.列名``），空列表表示库里的三档与种子完全一致。

    比对走 :class:`PlanSpec` 的全部字段而不是手写几个关键列——手写清单漏一列时，
    这行「看起来对」的错误永远浮不上来。差异做成字符串而不是布尔：运维要知道的
    是**哪一列**被改过，不是「不一致」三个字。
    """

    by_code: dict[str, Plan] = {}
    mismatches: list[str] = []
    for row in rows:
        if row.code not in PLAN_PRESETS:
            mismatches.append(f"{row.code}: 未知档位（不在 PLAN_PRESETS 里）")
        elif row.code in by_code:
            # 库里有唯一索引，正常到不了这里；手工修库修出两行同码时不能装作没看见。
            mismatches.append(f"{row.code}: 库里有多档同码")
        else:
            by_code[row.code] = row

    for code in PLAN_CODES:
        seeded = by_code.get(code)
        if seeded is None:
            mismatches.append(f"{code}: 库里没有这一档")
            continue
        expected = PLAN_PRESETS[code]
        mapped = plan_spec_of(seeded)
        for field in dataclasses.fields(PlanSpec):
            if getattr(mapped, field.name) != getattr(expected, field.name):
                mismatches.append(f"{code}.{field.name}")

    return mismatches
