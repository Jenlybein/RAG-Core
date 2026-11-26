"""表 6：plans —— 套餐（free / plus / pro / max）。

**这张表才是真相，本文件里的四档常量只是首次播种的种子。** 五个授权位、三本账额度、
``max_top_k`` 与规模上限都是面板上会出现输入框的东西，全部落在库里；若某列只活在代码
里，运营想给一个客户临时提额度就只能改代码发版，改错了也没有预览与审计——「界面上改得
动、后端读不到」比「界面上没有这个字段」更糟。

**业务列的清单只写一处**（:data:`PLAN_BUSINESS_COLUMNS`），它从 :class:`PlanSpec`
的字段推导出来：加一列 = 加一个 dataclass 字段（外加映射/缓存/控制面都从这里取，不必
各改一遍）。逐列对账由 :func:`preset_mismatches` 负责：播种之后把行映射回上下文，与种子
逐字段比对，**少一列就是可见的失败**。

**第 13 步起这张表没有「档位白名单」**：``allowed_profiles`` 列已删除——档位概念退役后
它没有可表达的东西了，「一次调用用多好」由密钥配置的三项直参数决定（ADR-0032）。

``plan_id`` 是整数自增主键，别的表只存这个数字（不加外键，见票面「裸字符串 id、不加
外键」的既有风格）；``code`` 唯一，是给人看与给脚本用的名字。想改用 UUID 或把四档塞进
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now

PLAN_FREE: Final[str] = "free"
PLAN_PLUS: Final[str] = "plus"
PLAN_PRO: Final[str] = "pro"
PLAN_MAX: Final[str] = "max"

# 四档的名字就是 ``code``：脚本入参、库里的行、错误信息里出现的都是这四个词之一。
PLAN_CODES: Final[tuple[str, ...]] = (PLAN_FREE, PLAN_PLUS, PLAN_PRO, PLAN_MAX)

#: 快照/行上不属于业务列的两个身份列。它们不上面板、不参与「上限与能力」的对账。
PLAN_IDENTITY_COLUMNS: Final[tuple[str, ...]] = ("code", "name")


class Plan(Base):
    __tablename__ = "plans"

    # 整数自增主键：引用方（tenants / accounts）只需要存一个数字。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # ── 五个授权位（与密钥配置同名列，本表是**上限**）。
    retrieve_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    decompose_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── 三本账的租户级那一半 + 唯一的质量封顶。
    retrieve_qps: Mapped[int] = mapped_column(Integer)
    retrieve_daily: Mapped[int] = mapped_column(Integer)
    rerank_qps: Mapped[int] = mapped_column(Integer, default=0)
    rerank_daily: Mapped[int] = mapped_column(Integer, default=0)
    llm_qps: Mapped[int] = mapped_column(Integer, default=0)
    llm_daily: Mapped[int] = mapped_column(Integer, default=0)
    #: 一次能取回多少（质量封顶）。0 = 不额外封顶，退回平台硬上限 ``MAX_TOP_K``。
    max_top_k: Mapped[int] = mapped_column(Integer, default=0)
    # ── 规模上限。
    max_kb: Mapped[int] = mapped_column(Integer)
    max_kb_per_retrieve: Mapped[int] = mapped_column(Integer)
    max_documents_per_kb: Mapped[int] = mapped_column(Integer)
    #: 单库累计正文 token 上限（#220 / ADR-0035 决策四）。**与文档数上限并存**：行数那一条
    #: 是廉价兜底（防「十万篇空文档」），token 这一条才是「装了多少内容」。
    max_tokens_per_kb: Mapped[int] = mapped_column(Integer)
    #: 每日切块额度（token，第四本账，ADR-0035 决策三/五）。索引期的 embedding 消耗按它管；
    #: 0 = 这项能力没买（无日额度）——目前四档都不是 0，0 留作将来「停用索引」的表达。
    chunk_daily_tokens: Mapped[int] = mapped_column(Integer)
    # **没有** ``max_processing_documents``：那条「同时处理中篇数」的配额已删除（#218，
    # 理由与代价见 ADR-0034 决策八——它量的「索引中」本身不可信，而入口拒绝也不是对
    # 「别人排队变长」的处置）。列的取值留档在 ADR-0034 决策八，迁移的 downgrade 会
    # 按那四档原值把列加回去。
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
    max_top_k: int
    max_kb: int
    max_kb_per_retrieve: int
    max_documents_per_kb: int
    max_tokens_per_kb: int
    chunk_daily_tokens: int


#: 快照的全部列名（字段顺序就是列顺序，``PLAN_SPEC_COLUMNS`` 因此也是行/缓存读取的清单）。
PLAN_SPEC_COLUMNS: Final[tuple[str, ...]] = tuple(
    field.name for field in dataclasses.fields(PlanSpec)
)

#: 面板上会出现输入框的业务列；对账、缓存、控制面映射与改套餐补丁校验都从这一处推导。
#: 顺序只影响对账报告的可读性，不参与任何判定。
PLAN_BUSINESS_COLUMNS: Final[tuple[str, ...]] = tuple(
    column for column in PLAN_SPEC_COLUMNS if column not in PLAN_IDENTITY_COLUMNS
)

#: 各列的声明类型（``from __future__ import annotations`` 让字段注解是字符串，
#: 所以用 ``get_type_hints`` 解析一次，别在每个调用点 ``field.type == "int"``）。
_PLAN_FIELD_TYPES: Final[dict[str, Any]] = get_type_hints(PlanSpec)

#: **允许为 0** 的数值列：0 在这里是「关闭 / 不额外封顶」，不是「额度填错」。
#: 与 ``retrieve_qps`` 的语义刻意相反——那条 0 等于当场停服，是必须拒绝的输入。
PLAN_ZEROABLE_COLUMNS: Final[tuple[str, ...]] = (
    # 三本账里的「加工能力」额度：0 = 这项能力没买（free 档就是 0）。
    "rerank_qps",
    "rerank_daily",
    "llm_qps",
    "llm_daily",
    # max_top_k=0 表示「不额外封顶，退回平台硬上限 MAX_TOP_K」，也是合法值。
    "max_top_k",
)

#: 改得到、需要校验「≥ 1」的数值上限。从快照字段的类型推导：加一列数值上限不必
#: 再改一份手写清单。四个能力开关是布尔列，没有「≥ 1」这道校验，所以不在这里；
#: :data:`PLAN_ZEROABLE_COLUMNS` 里的两列也不在——它们只要求 ≥ 0。
PLAN_NUMERIC_COLUMNS: Final[tuple[str, ...]] = tuple(
    column
    for column in PLAN_BUSINESS_COLUMNS
    if _PLAN_FIELD_TYPES[column] is int and column not in PLAN_ZEROABLE_COLUMNS
)


#: 四档的种子值。**只在 plans 表为空时写入**（见 ``identity_seeding.seed_plans``），
#: 之后库里的行才是真相——运营改过的额度不允许被下一次启动悄悄改回去。
#:
#: 倍率口径（spec #184）：plus 是基准（1×），pro = 5×，max = 20×；free 只开基线
#: ``retrieve`` 一个授权位，重排与大模型两本账的额度都是 0（等于没买这项能力）。
PLAN_PRESETS: Final[dict[str, PlanSpec]] = {
    PLAN_FREE: PlanSpec(
        code=PLAN_FREE,
        name="免费版",
        retrieve_allowed=True,
        rewrite_allowed=False,
        decompose_allowed=False,
        research_allowed=False,
        rerank_allowed=False,
        retrieve_qps=3,
        retrieve_daily=500,
        rerank_qps=0,
        rerank_daily=0,
        llm_qps=0,
        llm_daily=0,
        max_top_k=5,
        max_kb=3,
        max_kb_per_retrieve=1,
        max_documents_per_kb=30,
        max_tokens_per_kb=30_000,
        chunk_daily_tokens=300_000,
    ),
    PLAN_PLUS: PlanSpec(
        code=PLAN_PLUS,
        name="进阶版",
        retrieve_allowed=True,
        rewrite_allowed=True,
        decompose_allowed=True,
        research_allowed=True,
        rerank_allowed=True,
        retrieve_qps=10,
        retrieve_daily=5000,
        rerank_qps=3,
        rerank_daily=1000,
        llm_qps=1,
        llm_daily=300,
        max_top_k=10,
        max_kb=10,
        max_kb_per_retrieve=3,
        max_documents_per_kb=200,
        max_tokens_per_kb=300_000,
        chunk_daily_tokens=4_000_000,
    ),
    PLAN_PRO: PlanSpec(
        code=PLAN_PRO,
        name="专业版",
        retrieve_allowed=True,
        rewrite_allowed=True,
        decompose_allowed=True,
        research_allowed=True,
        rerank_allowed=True,
        retrieve_qps=50,
        retrieve_daily=25000,
        rerank_qps=15,
        rerank_daily=5000,
        llm_qps=5,
        llm_daily=1500,
        max_top_k=15,
        max_kb=50,
        max_kb_per_retrieve=5,
        max_documents_per_kb=5000,
        max_tokens_per_kb=2_000_000,
        chunk_daily_tokens=9_000_000,
    ),
    PLAN_MAX: PlanSpec(
        code=PLAN_MAX,
        name="旗舰版",
        retrieve_allowed=True,
        rewrite_allowed=True,
        decompose_allowed=True,
        research_allowed=True,
        rerank_allowed=True,
        retrieve_qps=200,
        retrieve_daily=100000,
        rerank_qps=60,
        rerank_daily=20000,
        llm_qps=20,
        llm_daily=6000,
        max_top_k=20,
        max_kb=100,
        max_kb_per_retrieve=5,
        max_documents_per_kb=5000,
        max_tokens_per_kb=5_000_000,
        chunk_daily_tokens=15_000_000,
    ),
}


def check_plan_code(code: str) -> str:
    """校验套餐名；不认识就立刻失败，并把已知的四档列出来。

    不提供「猜一个最接近的」或「回落到最低档」：改套餐时把 ``pro`` 拼错成 ``Pro``
    若被静默降级成免费档，客户会先掉额度再收到投诉，而日志里什么异常都没有。
    """

    if code not in PLAN_CODES:
        raise ValueError(f"未知套餐档位：{code}（可选：{'/'.join(PLAN_CODES)}）")
    return code


def plan_columns_of(source: object) -> dict[str, Any]:
    """按 :data:`PLAN_SPEC_COLUMNS` 从任何带同名属性的对象上取一份列值（行或快照）。

    逐列点名而不是 ``vars(source)``：ORM 行上还有 ``_sa_instance_state`` 这类内部状态，
    让它进入快照或审计只会把内部结构变成对外契约。缺属性就是 ``AttributeError``——
    不做兜底（映射漏一列必须当场炸，不能静静变成一个看起来合理的值）。
    """

    return {column: getattr(source, column) for column in PLAN_SPEC_COLUMNS}


def plan_spec_from_mapping(values: Mapping[str, Any]) -> PlanSpec:
    """按列名从映射造快照（ORM 行与缓存 JSON 都走这里）。

    少一列就是 ``KeyError``：缓存脏数据不许被静默补成兜底值。多一列被忽略。
    """

    kwargs = {column: values[column] for column in PLAN_SPEC_COLUMNS}
    # ``**dict[str, Any]`` 会让类型检查器左右为难；运行时的「缺列即 KeyError」才是这里
    # 要守的契约（见 tests/test_plan_presets.py 的映射兜底用例）。
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

    return Plan(**plan_columns_of(spec))


def preset_mismatches(rows: Iterable[Plan]) -> list[str]:
    """逐列对账：返回差异清单（``档位.列名``），空列表表示库里的四档与种子完全一致。

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
        for field_name in PLAN_SPEC_COLUMNS:
            if getattr(mapped, field_name) != getattr(expected, field_name):
                mismatches.append(f"{code}.{field_name}")

    return mismatches
