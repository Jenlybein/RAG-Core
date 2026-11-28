"""表 5：index_build_versions —— 一次索引构建 = 一个**不可变版本**（ADR-0025）。

索引不是「当前状态」，它是一次次构建的**产物**；这张表就是那批产物的身份行。状态机：

```text
building ──▶ ready ──(单事务切换)──▶ active ──▶ archived
    │                                  ▲            │
    │                                  └───(回滚)───┘
    └──▶ failed（线上仍查旧的 active，error_message 记原因）
```

两条约束**写进库**而不是写在注释里：同一库同一时刻最多一个 ``active``、最多一个
``building``（两条部分唯一索引），``ready`` 的前置条件是四项计数相等（#142 的激活流程守）。

**``archived`` 不是终态**（#155）：回滚就是 ``archived → active``，真值表与动作都只有一份
（``app.services.index_build_service``）。本票（#141）落地的是数据模型与库列；构建编排 /
激活事务见 #142，回滚见 #155——所以这里没有 ``mark_ready`` 之类的方法，写了就是「有一个
没人调用的状态迁移」，它比没有更容易让人以为流程已经存在。

``version`` 是**版本名**（``v1``、``v2``…），与 ``chunks.index_version`` 同宽同值域；
``document_count`` / ``chunk_count`` 是 ``ready`` 时的计数快照，用于激活前比对。

## 四列「进度」各说各的事（#214）

一次构建收不收尾，判据是**真值**（这一版这一稿下已经有切片的文档数），不是计数器
（[ADR-0034](../../docs/adr/0034-queue-state-and-build-convergence.md) 决策四）。所以这一行上
有四个容易混的数字，语义各不相同：

| 列 | 它是什么 | 谁读它 |
| --- | --- | --- |
| ``documents_planned`` | 起构建那一刻这个库里**可参与构建**的篇数快照 | 收敛判据的分母（唯一参与判定的） |
| ``doc_version`` | 这一轮构建统一按第几稿数 | 收敛判据的另一半（``该版本 + 该稿``） |
| ``documents_done`` | 进度：本轮真重建成功的篇数 | **只做进度条**，任何判定都不许读它 |
| ``document_count`` | ``ready`` 时的真值快照（这一版由几篇文档构成） | 报告 / 激活前的读数 |

``documents_done`` 与 ``document_count`` 在收尾那一刻相等，但**不许互相顶替**：前者会随着
重投、续建、清扫器来回变，后者是「这一版最后是几篇建的」。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Mapping, Sequence

import structlog
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.exceptions import AppError, ErrorCode
from app.db.base import Base, now
from app.models.knowledge_base import KnowledgeBase

logger = structlog.get_logger(__name__)

#: 五种状态。常量与迁移里的 CHECK 约束、将来的激活流程共用同一份取值。
INDEX_BUILD_BUILDING: Final[str] = "building"
INDEX_BUILD_READY: Final[str] = "ready"
INDEX_BUILD_ACTIVE: Final[str] = "active"
INDEX_BUILD_ARCHIVED: Final[str] = "archived"
INDEX_BUILD_FAILED: Final[str] = "failed"
INDEX_BUILD_STATUSES: Final[tuple[str, ...]] = (
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_READY,
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    INDEX_BUILD_FAILED,
)

#: 守卫拒绝检索时的那句固定话术（与「检索失败」区分开：这是服务端状态不一致）。
UNRESOLVED_INDEX_VERSION_MESSAGE: Final[str] = "index version not resolved"

#: 版本名的形状：``v1``、``v12``。**只认这一种**——版本名会变成物理索引名的后缀
#: （``rag_chunks__v2``）与 ``chunks.index_version`` 的值，随手写个 ``new`` 或
#: ``v2-old`` 之后没人能按名字排出版本顺序，也没法拼出物理索引名。
_VERSION_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v(\d+)$")


class IndexBuildVersion(Base):
    __tablename__ = "index_build_versions"
    __table_args__ = (
        # 同一库内版本名唯一：``(租户, 库, v1)`` 只能有一行。
        UniqueConstraint(
            "tenant_id", "kb_id", "version", name="uq_index_build_versions_tenant_kb_version"
        ),
        CheckConstraint(
            "status IN ('building', 'ready', 'active', 'archived', 'failed')",
            name="ck_index_build_versions_status",
        ),
        # 两条**部分**唯一索引：同一库同一时刻最多一个 active、一个 building。
        # 写成普通唯一索引会把「一个 archived 的历史版本」也算进去。
        Index(
            "uq_index_build_versions_active",
            "tenant_id",
            "kb_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_index_build_versions_building",
            "tenant_id",
            "kb_id",
            unique=True,
            postgresql_where=text("status = 'building'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    #: 版本名（``v1`` / ``v2``…），与 ``chunks.index_version`` 同宽（String(32)）。
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=INDEX_BUILD_BUILDING)
    #: ``ready`` 时的计数快照：两项从本库取，另两项（向量 / 关键词文档）在别的存储里，
    #: 由激活流程比对（#142）。构建中途它们可以是 NULL。
    document_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 这一版是用**哪些切块参数**构出来的（第 16 步 #207）。切块参数是索引期旋钮，
    #: 改了必须重建；记下来才能让控制台判断「库当前设置 ≠ 生效版本的参数」并标「需重建」。
    #: **可空**是刻意的：老版本行补不出来，NULL = 「这一版没留参数」，读侧按「不一致」处理。
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ── #214：构建收敛用的四列（语义见模块文档的表格）────────────────────────
    #
    #: 参与篇数快照：起构建那一刻该库 ``status=1`` 的文档数。
    #:
    #: **为什么必须是快照，而不是收尾时现数**：构建要跑很久，中途有人上传或删除文档；
    #: 现数会让分母跟着漂——漂低会让「其实缺了几篇」的真值显得够（空版本上线的老路），
    #: 漂高会让一次正常的构建永远收不了尾（ADR-0034 决策四）。
    documents_planned: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    #: 稿号快照：这一轮构建统一按第几稿数（与 ``chunks.doc_version`` 同一个口径）。
    #:
    #: 取「起构建那一刻各文档当前稿指针的最大值」：库里的文档各自演进，而一次构建产出的
    #: 切片要么都算这一稿、要么都不算——混着数的后果是「同一批切片一半旧稿一半新稿」，
    #: 检索按当前稿过滤时看不出来，事后按稿对账才发现（ADR-0031 决策七）。
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    #: 已完成数：本轮**真的**重建成功的篇数（抢不到的不计）。**只服务进度条**。
    documents_done: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    #: 收敛期限：这一行**最晚什么时候必须有人给它一个结论**（构建行自己的租约）。
    #:
    #: 它不是「第几篇何时跑完」，而是一个把「等」变成有限区间的死线：过期之后任何一方
    #: 都该按真值收尾（ADR-0034 决策五）。``server_default`` 取插入那一刻，是因为「没写
    #: 期限」最保守的解释就是「已经该收尾了」——宁可让清扫器早一点看一眼，也不留一个
    #: 永远等不到结论的 ``building`` 行。
    converge_deadline: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("now()")
    )

    # ── #215：按篇投递之后新增的两列 ────────────────────────────────────────
    #
    #: 「未入新版本」的明细：``[{"document_id", "title", "reason"}]``。
    #:
    #: **为什么放在构建行而不是文档行**（ADR-0034 决策九）：
    #:
    #: * 它是**这一版**的属性，不是文档的属性——同一篇可能「没进 v2、进了 v3」，挂在文档行上
    #:   就得每来一版改写一次，语义迟早变脏；
    #: * 它天然就是**续建的清单**：清扫器读这一格就知道缺哪几篇、为什么缺；
    #: * 版本列表页直接能显示「497 / 500 篇，3 篇未入」，比在文档列表里插一堆红旗更好读。
    #:
    #: 构建子任务失败时**不改文档状态**（那篇文档本身没坏，线上旧版本照常可查），只在这里
    #: 追加一条。空列表与非空列表之别就是收敛判据里 ``failed`` 那一项。
    failed_documents: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: 这一版收敛成 ``ready`` 之后要不要**立刻激活**（#215 的回归修法，迁移 ``f1a4b8d6c392``）。
    #:
    #: 只有「管理员改了切块设置」那条路径置真——它的语义是「改完就该生效」。存在构建行上
    #: 而不是放进每篇子任务的载荷：收敛可以被任意一方触发（最后一篇 / 清扫器 / 运维脚本），
    #: 它们读同一格，行为才一致。
    auto_activate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    #: 清扫器重投过几次（ADR-0034 决策七的兜底上限）。超过上限就收成 ``failed``——
    #: 「一直重投也救不回来」必须有个尽头，否则一个坏构建会把清扫器拖成永动机。
    redispatch_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )


class IndexVersionUnresolved(AppError):
    """守卫：有库还没有生效版本，**在启用版本过滤之前**把这次检索显式拦下。

    为什么不是「跳过它、按空范围查」：``(kb AND version)`` 一条都匹配不上时链路会
    返回一次诚实的空结果，而那个空结果会被包装成「本次没命中」——故障看起来像查询问题
    （ADR-0025 决策三的原话）。宁可 5xxxx 一次，也不要一个「不报错但搜不到」。

    单独一个类而不是裸 ``AppError``：调用方 / 测试要能把「服务端漏了回填」与
    「关键词引擎挂了」区分开，这两件事的处置完全不同。
    """

    def __init__(self, kb_ids: Sequence[str]) -> None:
        self.kb_ids = tuple(kb_ids)
        super().__init__(ErrorCode.SERVER_ERROR, UNRESOLVED_INDEX_VERSION_MESSAGE)


def effective_index_version(kb: KnowledgeBase) -> str | None:
    """取该库当前生效的索引版本；没有就是 ``None``（不是空串、不是 ``v1``）。"""

    version = getattr(kb, "active_index_version", None)
    if version is None:
        return None
    value = str(version).strip()
    return value or None


def version_number(version: str) -> int | None:
    """``v12`` → ``12``；不是 ``v<数字>`` 的写法回 ``None``。

    回 ``None`` 而不是抛错，也不是猜一个序：排不出来只意味着「这个名字不参与排序」，
    调用方（:func:`next_version_name`）跳过它即可。抛错会让一行手写坏数据把整条构建
    流程卡死，猜序号则可能拼出一个**已被占用**的名字。
    """

    matched = _VERSION_NAME_PATTERN.match(version.strip())
    if matched is None:
        return None
    return int(matched.group(1))


def next_version_name(versions: Sequence[str]) -> str:
    """下一个版本名 = **已用最大编号 + 1**；一个都没有时 ``v1``。

    为什么不是 ``len(versions) + 1``：删过版本、或跳过编号之后，两者会撞名，而撞名
    的表现是一条唯一约束错误——构建还没开始就失败，且没人看得出是名字算错了。
    ``max + 1`` 天然单调，即使历史上删过 ``v2`` 也不会把新版本叫成 ``v2``。
    """

    numbers = [number for number in map(version_number, versions) if number is not None]
    return f"v{max(numbers) + 1 if numbers else 1}"


def resolve_index_versions(kbs: Sequence[KnowledgeBase]) -> Mapping[str, str]:
    """把**本次已经取到的知识库行**解析成检索范围：``kb_id → 生效版本``。

    这是检索范围**唯一的构造点**（ADR-0025 决策四）。它必须是纯函数：

    * 不查库——行的 ``settings`` / 名称已经在可见性校验里取回来了，再查一次就把
      「每库一次」变成「每库两次」；
    * 不读请求——版本由服务端决定，**永远不是请求参数**（否则调用方能让检索回到旧切法）；
    * 不读配置——没有「默认版本」这个旋钮，缺就拦。

    **读路径只用它的键**（现场九·第四步）：范围的形状从「库 → 生效版本」的配对条件改成
    ``kb_id IN (...)``，可见性交给篇级当前代。值（版本）现在只有两个用处：归属索引那一列，
    以及下面这条守卫。

    任何一行没有生效版本 → :class:`IndexVersionUnresolved`。这条守卫守的是**回填漏做**，
    与可见性无关：迁移/建库漏了回填时运维看到的是明确的一次 5xxxx，而不是一份「看起来像
    没命中」的空结果。它随第二支迁移删列一起退场（那时新库只按代建）。
    """

    versions: dict[str, str] = {}
    unresolved: list[str] = []
    for kb in kbs:
        kb_id = str(kb.id)
        version = effective_index_version(kb)
        if version is None:
            unresolved.append(kb_id)
            continue
        versions[kb_id] = version

    if unresolved:
        # 只记 id，不记问句原文（归属索引 / 日志的同一条纪律）。
        logger.warning(
            "index version not resolved",
            kb_ids=unresolved,
            hint="run the index build backfill before enabling version filtering",
        )
        raise IndexVersionUnresolved(unresolved)

    return versions


__all__ = [
    "INDEX_BUILD_ACTIVE",
    "INDEX_BUILD_ARCHIVED",
    "INDEX_BUILD_BUILDING",
    "INDEX_BUILD_FAILED",
    "INDEX_BUILD_READY",
    "INDEX_BUILD_STATUSES",
    "IndexBuildVersion",
    "IndexVersionUnresolved",
    "UNRESOLVED_INDEX_VERSION_MESSAGE",
    "effective_index_version",
    "next_version_name",
    "resolve_index_versions",
    "version_number",
]
