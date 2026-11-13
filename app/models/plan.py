"""表 6：plans —— 套餐（free / standard / pro）。

**这张表才是真相，本文件里的三档常量只是首次播种的种子。** 上限与能力开关是面板上
会出现输入框的东西，全部落在库里；若某列只活在代码里，运营想给一个客户临时提额度就
只能改代码发版，改错了也没有预览与审计——「界面上改得动、后端读不到」比「界面上没有
这个字段」更糟。

**12 个业务列的清单只写一处**（:data:`PLAN_BUSINESS_COLUMNS`），它从 :class:`PlanSpec`
的字段推导出来：加一列 = 加一个 dataclass 字段（外加映射/缓存/控制面都从这里取，不必
各改一遍）。逐列对账由 :func:`preset_mismatches` 负责：播种之后把行映射回上下文，与种子
逐字段比对，**少一列就是可见的失败**。

``plan_id`` 是整数自增主键，别的表只存这个数字（不加外键，见票面「裸字符串 id、不加
外键」的既有风格）；``code`` 唯一，是给人看与给脚本用的名字。想改用 UUID 或把三档塞进
一张字典表都会破坏「一档一列、面板一字段一列」的对应关系——主键形态是**刻意的决定**，
不是遗留：调用方看到的永远是 ``code``，没见过这个整数（见架构文档本步一节）。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, get_type_hints

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

#: 快照/行上不属于业务列的两个身份列。它们不上面板、不参与「上限与能力」的对账。
PLAN_IDENTITY_COLUMNS: Final[tuple[str, ...]] = ("code", "name")


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
    # 五个能力开关与六个上限：拆成独立列而不是一个大 JSONB，是因为面板要逐字段渲染、
    # 校验、写审计；塞进 JSONB 之后，「这次改了哪一项」就又变成要解析的内容了。
    hybrid_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    query_rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 切片编排（第 11 步 B）：套餐层的「允许」。它是三层里的第一层，另外两层在
    # ``key_policies.orchestration_allowed``（这套集成要不要）与
    # ``profile_presets.orchestration``（这一档平时的默认）。三列同名不同义，刻意分成三张表：
    # 它们回答的是三个互不重叠的问题（ADR-0018）。
    orchestration_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
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
    orchestration_allowed: bool
    retrieve_qps: int
    retrieve_daily: int
    max_kb: int
    max_kb_per_retrieve: int
    max_documents_per_kb: int
    max_processing_documents: int


#: 快照的全部列名（字段顺序就是列顺序，``PLAN_SPEC_COLUMNS`` 因此也是行/缓存读取的清单）。
PLAN_SPEC_COLUMNS: Final[tuple[str, ...]] = tuple(
    field.name for field in dataclasses.fields(PlanSpec)
)

#: 面板上会出现输入框的 12 列；对账、缓存、控制面映射与改套餐补丁校验都从这一处推导。
#: 顺序只影响对账报告的可读性，不参与任何判定。
PLAN_BUSINESS_COLUMNS: Final[tuple[str, ...]] = tuple(
    column for column in PLAN_SPEC_COLUMNS if column not in PLAN_IDENTITY_COLUMNS
)

#: 各列的声明类型（``from __future__ import annotations`` 让字段注解是字符串，
#: 所以用 ``get_type_hints`` 解析一次，别在每个调用点 ``field.type == "int"``）。
_PLAN_FIELD_TYPES: Final[dict[str, Any]] = get_type_hints(PlanSpec)

#: 改得到、需要校验「≥ 1」的六个数值上限。从快照字段的类型推导：加一列数值上限不必
#: 再改一份手写清单。四个能力开关是布尔列，没有「≥ 1」这道校验，所以不在这里。
PLAN_NUMERIC_COLUMNS: Final[tuple[str, ...]] = tuple(
    column
    for column in PLAN_BUSINESS_COLUMNS
    if _PLAN_FIELD_TYPES[column] is int
)


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
        orchestration_allowed=False,
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
        orchestration_allowed=False,
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
        orchestration_allowed=True,
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


def plan_columns_of(source: object) -> dict[str, Any]:
    """按 :data:`PLAN_SPEC_COLUMNS` 从任何带同名属性的对象上取一份列值（行或快照）。

    逐列点名而不是 ``vars(source)``：ORM 行上还有 ``_sa_instance_state`` 这类内部状态，
    让它进入快照或审计只会把内部结构变成对外契约。
    """

    return {column: getattr(source, column) for column in PLAN_SPEC_COLUMNS}


def plan_spec_from_mapping(values: Mapping[str, Any]) -> PlanSpec:
    """按列名从映射造快照（ORM 行与缓存 JSON 都走这里）。

    ``allowed_profiles`` 在库里/JSON 里是数组，在快照里是元组——转换只在这里做一次。
    少一列就是 ``KeyError``（缓存脏数据不许被静默补成兜底值），多一列被忽略。
    """

    kwargs: dict[str, Any] = {column: values[column] for column in PLAN_SPEC_COLUMNS}
    kwargs["allowed_profiles"] = tuple(kwargs["allowed_profiles"])
    # ``**dict[str, Any]`` 会让类型检查器左右为难；运行时的「缺列即 TypeError」才是这里
    # 要守的契约（见 tests/test_plan_presets.py 的构造式兜底用例）。
    return PlanSpec(**kwargs)


def plan_spec_of(row: Plan) -> PlanSpec:
    """把一行套餐映射成上下文快照。

    **不做 ``bool()`` / ``int()`` 之类的类型兜底**：行里某列是 ``NULL`` 时，兜底会把
    它变成 ``False`` 或 ``0``，而最低档的四个开关恰好都是 ``False``——于是一个「播种时
    漏了这一列」的错误会在对账里静默通过。原样取值，``None`` 才会与常量比出差异。
    """

    return plan_spec_from_mapping(plan_columns_of(row))


def plan_row_of(spec: PlanSpec) -> Plan:
    """种子 → 待插入的行。只给「表为空时的首次播种」用，别拿它覆盖已有行。"""

    values: dict[str, Any] = plan_columns_of(spec)
    values["allowed_profiles"] = list(values["allowed_profiles"])
    return Plan(**values)


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
