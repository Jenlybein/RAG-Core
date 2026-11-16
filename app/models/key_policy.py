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

    # 第 13 步 I 阶段：这些列已物理删除。构造器仍接受它们（测试夹具 / 旧脚本的调用
    # 方式），值存进影子属性供 policy_snapshot_of 读取，**不落库、不进 ORM 映射**。
    _DEPRECATED_KWARGS = frozenset(
        {
            "hybrid_allowed",
            "query_rewrite_allowed",
            "profile",
            "custom_options",
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
        """已删列的属性读回落到影子值（旧代码/测试夹具），没有则按标准 AttributeError。"""

        if name in self._DEPRECATED_KWARGS:
            shadow = self.__dict__.get("_deprecated", {})
            if name in shadow:
                return shadow[name]
            # 未显式给过的废弃字段返回中性值，而不是报错——它们是 no-op 概念。
            return {
                "hybrid_allowed": False,
                "query_rewrite_allowed": False,
                "profile": "",
                "custom_options": {},
                "research_qps": 0,
                "research_daily": 0,
            }[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        """已删列的写入落到影子（旧脚本用 setattr 改 profile 等仍能生效）。"""

        if name in self._DEPRECATED_KWARGS and "_deprecated" in self.__dict__:
            self.__dict__["_deprecated"][name] = value
        else:
            super().__setattr__(name, value)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 裸字符串、不加外键：与既有四张业务表的风格一致，引用完整性由应用层校验。
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 数据范围：这套集成能查哪些库。空列表表示「还没圈定」，不是「全部」——
    # 「全部」一旦成为空列表的含义，把白名单清空就成了开闸。
    allowed_kb_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 能力开关：租户在套餐上限内给这套集成的分配。第 13 步删掉 hybrid/query_rewrite
    # 两个旧开关（质量/加工维度已不在这里表达），只留授权位。
    research_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 第 13 步 T1 迁移加的列（与 plans 同形，max_top_k 只在套餐上）。
    retrieve_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    rewrite_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    decompose_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_qps: Mapped[int] = mapped_column(Integer, default=0)
    rerank_daily: Mapped[int] = mapped_column(Integer, default=0)
    llm_qps: Mapped[int] = mapped_column(Integer, default=0)
    llm_daily: Mapped[int] = mapped_column(Integer, default=0)
    # 限额：Key 级计数卡住单个外部应用，租户级聚合由套餐上限卡住（两级计数）。
    retrieve_qps: Mapped[int] = mapped_column(Integer)
    retrieve_daily: Mapped[int] = mapped_column(Integer)
    # 第 13 步 I 阶段：三项参数从 custom_options 搬成真列（物理删档位存储）。
    retrieval_mode: Mapped[str] = mapped_column(String(16), default="vector")
    retrieval_top_k: Mapped[int] = mapped_column(Integer, default=5)
    retrieval_rerank: Mapped[bool] = mapped_column(Boolean, default=False)
    # 结果形状偏好：折叠成整节 / 表格行折回整表。默认都开（与既有响应默认一致）。
    include_parent: Mapped[bool] = mapped_column(Boolean, default=True)
    expand_table_rows: Mapped[bool] = mapped_column(Boolean, default=True)
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

    ``unrestricted_scope`` 是「这把 Key 不做数据范围收窄」的**显式标记**，默认
    ``False``（受限）。它存在是因为内置开发身份没有白名单可言，而 ``allowed_kb_ids``
    的空元组在仓库里的含义是「什么都没圈定、所以什么都不许」——让同一个空值兼职表达
    「全部」会让「清空白名单」变成开闸。真实配置经 :func:`policy_snapshot_of` 映射
    出来永远是受限的；只有合成的开发快照把它置为 ``True``。
    """

    allowed_kb_ids: tuple[str, ...]
    research_allowed: bool
    rerank_allowed: bool
    retrieve_qps: int
    retrieve_daily: int
    include_parent: bool
    expand_table_rows: bool
    #: 第 13 步三项参数的真值（从真列读）。None = 旧夹具未显式给，回退 custom_options。
    retrieval_mode: str | None = None
    retrieval_top_k: int | None = None
    retrieval_rerank: bool | None = None
    # ── deprecated 兼容字段：物理列已删，保留仅为守住旧测试/缓存构造点，不参与判定。
    hybrid_allowed: bool = False
    query_rewrite_allowed: bool = False
    research_qps: int = 0
    research_daily: int = 0
    profile: str = ""
    custom_options: tuple[tuple[str, Any], ...] = ()
    #: 只有内置开发身份的那份快照把它打开（见类文档末段）；缺省 = 受限。
    unrestricted_scope: bool = False
    # ── 第 13 步 T1 的新列（expand 段）。带缺省值让既有构造点不炸；
    # retrieve 缺省 True（基线能力），另两个新增加工位缺省 True 由迁移回填 false 覆盖。
    retrieve_allowed: bool = True
    rewrite_allowed: bool = True
    decompose_allowed: bool = True
    rerank_qps: int = 0
    rerank_daily: int = 0
    llm_qps: int = 0
    llm_daily: int = 0


def _expand_bit(row: object, name: str, *, default: bool) -> bool:
    """读第 13 步新位；未 flush 的 ORM 行 / 缺属性的夹具按缺省填（expand 段过渡）。"""

    value = getattr(row, name, None)
    return default if value is None else bool(value)


def _expand_int(row: object, name: str) -> int:
    """读第 13 步新账列；None 按 0 填。"""

    value = getattr(row, name, None)
    return 0 if value is None else int(value)


def _param_from_row(row: object, column: str, key: str, default: Any) -> Any:
    """第 13 步 I 阶段：先读真列；未 flush 的行 / 旧夹具回退到 deprecated custom_options。"""

    value = getattr(row, column, None)
    if value is not None:
        return value
    options = getattr(row, "custom_options", None) or {}
    if isinstance(options, dict) and key in options:
        return options[key]
    return default


def policy_snapshot_of(row: KeyPolicy) -> KeyPolicySnapshot:
    """把一行密钥配置映射成快照；三项参数从真列读，旧列留空兼容字段。"""

    research_allowed = _expand_bit(row, "research_allowed", default=False)
    rerank_allowed = _expand_bit(row, "rerank_allowed", default=False)
    shadow = getattr(row, "_deprecated", {}) or {}

    def _shadow(name: str, default: Any) -> Any:
        return shadow.get(name, default)

    raw_options = shadow.get("custom_options") or getattr(row, "custom_options", None) or {}
    has_shadow_params = isinstance(raw_options, dict) and bool(raw_options)

    if has_shadow_params:
        # 旧夹具/旧缓存只给 custom_options：三项参数以它为准（真库行 custom_options
        # 已物理删除，不会有影子值，永远走下面的真列分支）。
        mode = raw_options.get("mode", "vector")
        top_k = raw_options.get("top_k", 5)
        rerank = raw_options.get("rerank", False)
        # 真列字段留 None，让 profile_chain_of 走 custom_options 兜底路径。
        param_mode: str | None = None
        param_top_k: int | None = None
        param_rerank: bool | None = None
    else:
        param_mode = str(_param_from_row(row, "retrieval_mode", "mode", "vector"))
        param_top_k = int(_param_from_row(row, "retrieval_top_k", "top_k", 5))
        param_rerank = bool(_param_from_row(row, "retrieval_rerank", "rerank", False))

    return KeyPolicySnapshot(
        allowed_kb_ids=tuple(row.allowed_kb_ids),
        research_allowed=research_allowed,
        rerank_allowed=rerank_allowed,
        retrieve_qps=row.retrieve_qps,
        retrieve_daily=row.retrieve_daily,
        include_parent=row.include_parent,
        expand_table_rows=row.expand_table_rows,
        retrieval_mode=param_mode,
        retrieval_top_k=param_top_k,
        retrieval_rerank=param_rerank,
        # deprecated 兼容字段：物理列已删，值来自构造器影子属性（旧夹具）或默认。
        hybrid_allowed=bool(_shadow("hybrid_allowed", False)),
        query_rewrite_allowed=bool(_shadow("query_rewrite_allowed", False)),
        research_qps=int(_shadow("research_qps", 0)),
        research_daily=int(_shadow("research_daily", 0)),
        profile=str(_shadow("profile", "") or ""),
        custom_options=tuple(sorted(raw_options.items())) if isinstance(raw_options, dict) else (),
        retrieve_allowed=_expand_bit(row, "retrieve_allowed", default=True),
        rewrite_allowed=_expand_bit(row, "rewrite_allowed", default=False),
        decompose_allowed=_expand_bit(row, "decompose_allowed", default=False),
        rerank_qps=_expand_int(row, "rerank_qps"),
        rerank_daily=_expand_int(row, "rerank_daily"),
        llm_qps=_expand_int(row, "llm_qps"),
        llm_daily=_expand_int(row, "llm_daily"),
    )
