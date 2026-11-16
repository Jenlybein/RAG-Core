"""表 6：plans —— 套餐（free / standard / pro）。

**这张表才是真相，本文件里的三档常量只是首次播种的种子。** 上限与能力开关是面板上
会出现输入框的东西，全部落在库里；若某列只活在代码里，运营想给一个客户临时提额度就
只能改代码发版，改错了也没有预览与审计——「界面上改得动、后端读不到」比「界面上没有
这个字段」更糟。

**13 个业务列的清单只写一处**（:data:`PLAN_BUSINESS_COLUMNS`），它从 :class:`PlanSpec`
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
PLAN_PLUS: Final[str] = "plus"
PLAN_PRO: Final[str] = "pro"
PLAN_MAX: Final[str] = "max"

#: 第 13 步：standard 改名为 plus；保留别名常量让旧调用点继续编译（值指向 plus）。
PLAN_STANDARD: Final[str] = PLAN_PLUS

# 四档的名字就是 ``code``：脚本入参、库里的行、错误信息里出现的都是这四个词之一。
PLAN_CODES: Final[tuple[str, ...]] = (PLAN_FREE, PLAN_PLUS, PLAN_PRO, PLAN_MAX)

#: 快照/行上不属于业务列的两个身份列。它们不上面板、不参与「上限与能力」的对账。
PLAN_IDENTITY_COLUMNS: Final[tuple[str, ...]] = ("code", "name")


class Plan(Base):
    __tablename__ = "plans"

    # 第 13 步 I 阶段：这些列已物理删除。构造器兼容旧调用点，值存影子属性不落库。
    _DEPRECATED_KWARGS = frozenset(
        {
            "allowed_profiles",
            "hybrid_allowed",
            "query_rewrite_allowed",
            "research_qps",
            "research_daily",
        }
    )

    def __init__(self, **kwargs: Any) -> None:
        self._deprecated: dict[str, Any] = {
            key: kwargs.pop(key)
            for key in tuple(kwargs)
            if key in self._DEPRECATED_KWARGS
        }
        super().__init__(**kwargs)

    def __getattr__(self, name: str) -> Any:
        """已删列的属性读回落到影子值（旧代码/测试夹具）。"""

        if name in self._DEPRECATED_KWARGS:
            shadow = self.__dict__.get("_deprecated", {})
            if name in shadow:
                return shadow[name]
            return {
                "allowed_profiles": [],
                "hybrid_allowed": False,
                "query_rewrite_allowed": False,
                "research_qps": 0,
                "research_daily": 0,
            }[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        """已删列的写入落到影子（旧控制面用 setattr 改 allowed_profiles 等仍能生效）。"""

        if name in self._DEPRECATED_KWARGS and "_deprecated" in self.__dict__:
            self.__dict__["_deprecated"][name] = value
        else:
            super().__setattr__(name, value)

    # 整数自增主键：引用方（tenants / 将来别的表）只需要存一个数字。用 uuid 表达
    # 「三档」只是让每行多 32 个字符，还得额外维护 code 唯一。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 第 13 步 I 阶段：allowed_profiles / hybrid_allowed / query_rewrite_allowed 与
    # research_qps / research_daily 五列物理删除（档位存储 + 旧能力列）。
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 第 13 步 T1 迁移加的列。retrieve_allowed 缺省 True（基线能力，存量行不静默停服）。
    retrieve_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    decompose_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_qps: Mapped[int] = mapped_column(Integer, default=0)
    rerank_daily: Mapped[int] = mapped_column(Integer, default=0)
    llm_qps: Mapped[int] = mapped_column(Integer, default=0)
    llm_daily: Mapped[int] = mapped_column(Integer, default=0)
    max_top_k: Mapped[int] = mapped_column(Integer, default=0)
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
    # 第 13 步 I 阶段：以下五列物理已删，留作 deprecated 兼容字段（默认空/0）。
    allowed_profiles: tuple[str, ...] = ()
    hybrid_allowed: bool = False
    query_rewrite_allowed: bool = False
    research_qps: int = 0
    research_daily: int = 0
    # 仍存在的业务列。
    rerank_allowed: bool = False
    research_allowed: bool = False
    retrieve_qps: int = 0
    retrieve_daily: int = 0
    max_kb: int = 0
    max_kb_per_retrieve: int = 0
    max_documents_per_kb: int = 0
    max_processing_documents: int = 0
    # ── 第 13 步 T1 的新列：三个新增端点位 + 两本新账四列 + max_top_k。
    # ``research_allowed`` / ``rerank_allowed`` 复用已有字段。带缺省值让所有既有
    # 构造点不炸（expand 段）；T8 切真相源时再收紧。
    retrieve_allowed: bool = True
    rewrite_allowed: bool = True
    decompose_allowed: bool = True
    rerank_qps: int = 0
    rerank_daily: int = 0
    llm_qps: int = 0
    llm_daily: int = 0
    max_top_k: int = 0


#: 第 13 步 I 阶段：档位存储列已物理删除，这些字段只在快照里作 deprecated 兼容壳，
#: 不参与行映射 / 缓存 / 对账（否则会去读库里已不存在的列）。
_DEPRECATED_PLAN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "allowed_profiles",
        "hybrid_allowed",
        "query_rewrite_allowed",
        "research_qps",
        "research_daily",
    }
)

#: 快照的全部列名（字段顺序就是列顺序，``PLAN_SPEC_COLUMNS`` 因此也是行/缓存读取的清单）。
PLAN_SPEC_COLUMNS: Final[tuple[str, ...]] = tuple(
    field.name
    for field in dataclasses.fields(PlanSpec)
    if field.name not in _DEPRECATED_PLAN_FIELDS
)

#: 面板上会出现输入框的 13 列；对账、缓存、控制面映射与改套餐补丁校验都从这一处推导。
#: 顺序只影响对账报告的可读性，不参与任何判定。
PLAN_BUSINESS_COLUMNS: Final[tuple[str, ...]] = tuple(
    column for column in PLAN_SPEC_COLUMNS if column not in PLAN_IDENTITY_COLUMNS
)

#: 各列的声明类型（``from __future__ import annotations`` 让字段注解是字符串，
#: 所以用 ``get_type_hints`` 解析一次，别在每个调用点 ``field.type == "int"``）。
_PLAN_FIELD_TYPES: Final[dict[str, Any]] = get_type_hints(PlanSpec)

#: **允许为 0** 的两个数值列（票 #150）：0 在这里是「关闭」，不是「额度填错」。
#: 与 ``retrieve_qps`` 的语义刻意相反——那条 0 等于当场停服，是必须拒绝的输入。
PLAN_ZEROABLE_COLUMNS: Final[tuple[str, ...]] = (
    "research_qps",
    "research_daily",
    # 第 13 步：三本账里的「新增加工能力」额度允许为 0（= 这项能力没买）。
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
PLAN_PRESETS: Final[dict[str, PlanSpec]] = {
    PLAN_FREE: PlanSpec(
        code=PLAN_FREE,
        name="免费版",
        allowed_profiles=("speed",),
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        research_allowed=False,
        retrieve_qps=3,
        retrieve_daily=500,
        research_qps=0,
        research_daily=0,
        max_top_k=5,
        max_kb=1,
        max_kb_per_retrieve=1,
        max_documents_per_kb=30,
        max_processing_documents=1,
    ),
    PLAN_PLUS: PlanSpec(
        code=PLAN_PLUS,
        name="进阶版",
        allowed_profiles=("speed", "balanced", "custom"),
        hybrid_allowed=True,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        research_allowed=False,
        retrieve_qps=10,
        retrieve_daily=5000,
        research_qps=0,
        research_daily=0,
        max_top_k=10,
        max_kb=5,
        max_kb_per_retrieve=3,
        max_documents_per_kb=200,
        max_processing_documents=2,
    ),
    PLAN_PRO: PlanSpec(
        code=PLAN_PRO,
        name="专业版",
        allowed_profiles=("speed", "balanced", "quality", "custom"),
        hybrid_allowed=True,
        rerank_allowed=True,
        query_rewrite_allowed=True,
        research_allowed=True,
        retrieve_qps=50,
        retrieve_daily=100000,
        research_qps=1,
        research_daily=100,
        max_top_k=15,
        max_kb=50,
        max_kb_per_retrieve=5,
        max_documents_per_kb=5000,
        max_processing_documents=10,
    ),
    PLAN_MAX: PlanSpec(
        code=PLAN_MAX,
        name="旗舰版",
        allowed_profiles=("speed", "balanced", "quality", "custom"),
        hybrid_allowed=True,
        rerank_allowed=True,
        query_rewrite_allowed=True,
        research_allowed=True,
        retrieve_qps=200,
        retrieve_daily=100000,
        research_qps=1,
        research_daily=100,
        max_top_k=20,
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

    # 第 13 步 expand 段：新列在部分测试夹具（SimpleNamespace）上可能不存在，
    # 按 _EXPAND_DEFAULTS 兜一次；真 ORM 行一定有这些列。
    columns = {
        column: getattr(source, column, _EXPAND_DEFAULTS.get(column))
        for column in PLAN_SPEC_COLUMNS
    }
    # deprecated 字段不落库，但兼容壳需要读到值：
    # - ORM 行的旧值存在影子属性里；
    # - PlanSpec 快照/种子里它们是真实属性。
    shadow = getattr(source, "_deprecated", {}) or {}
    for name in _DEPRECATED_PLAN_FIELDS:
        if name in shadow:
            columns[name] = shadow[name]
        elif hasattr(source, name):
            columns[name] = getattr(source, name)
    return columns


#: 第 13 步 expand 段：这些新列在**旧缓存 JSON**里可能不存在，允许按缺省填充；
#: 其余旧列缺一列仍是 ``KeyError``（缓存脏数据不许被静默补成兜底值）。
_EXPAND_DEFAULTS: Final[dict[str, Any]] = {
    "retrieve_allowed": True,
    "rewrite_allowed": True,
    "decompose_allowed": True,
    "rerank_qps": 0,
    "rerank_daily": 0,
    "llm_qps": 0,
    "llm_daily": 0,
    "max_top_k": 0,
}


def plan_spec_from_mapping(values: Mapping[str, Any]) -> PlanSpec:
    """按列名从映射造快照（ORM 行与缓存 JSON 都走这里）。

    ``allowed_profiles`` 在库里/JSON 里是数组，在快照里是元组——转换只在这里做一次。
    旧列少一列就是 ``KeyError``；第 13 步新列允许按 :data:`_EXPAND_DEFAULTS` 填充
    （旧缓存里没有它们，不该让升级后的读取当场失败）。多一列被忽略。
    """

    kwargs: dict[str, Any] = {}
    for column in PLAN_SPEC_COLUMNS:
        if column in values:
            kwargs[column] = values[column]
        elif column in _EXPAND_DEFAULTS:
            kwargs[column] = _EXPAND_DEFAULTS[column]
        else:
            raise KeyError(column)
    # 第 13 步：五个 deprecated 字段不落库，但影子属性/旧缓存里带着值时原样读进兼容壳。
    if "allowed_profiles" in values:
        kwargs["allowed_profiles"] = tuple(values["allowed_profiles"])
    for name in ("hybrid_allowed", "query_rewrite_allowed", "research_qps", "research_daily"):
        if name in values:
            kwargs[name] = values[name]
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
    # 第 13 步：deprecated 字段的值由 Plan.__init__ 收进影子属性，不落库。
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
        # 第 13 步：只对账真实存在的列；deprecated 兼容字段（旧档位存储）不比对。
        for field_name in PLAN_SPEC_COLUMNS:
            if getattr(mapped, field_name) != getattr(expected, field_name):
                mismatches.append(f"{code}.{field_name}")

    return mismatches
