"""索引构建编排（#142）：一次构建 = 一个不可变版本的生命周期（ADR-0025）。

```text
building ──▶ ready ──(单事务切换)──▶ active ──▶ archived
    │                                  ▲            │
    │                                  └───(回滚)───┘
    └──▶ failed（线上仍查旧的 active，error_message 记原因）
```

**本模块是状态机的唯一实现点**：谁能迁到谁写在 :data:`ALLOWED_TRANSITIONS` 里，而不是
散在若干处 ``row.status = ...``。新增一条迁移必须先改那张表——那是一行会被人看见的代码，
而散在各处的赋值只会让人以为「状态怎么变的」要去猜。

**``archived`` 不是终态**（#155）：回滚就是 ``archived → active``，而它走的**不是**另一套
动作——:meth:`IndexBuildService.activate` 与 :meth:`IndexBuildService.rollback` 都进
:meth:`IndexBuildService._switch_active_version`，唯一的差别是「从哪个版本出发」。

## 构建期间线上照旧查旧版本（决策二）

构建只写**目标版本**（``chunks.index_version`` / 关键词的物理索引都带它），知识库上的
``active_index_version`` 在整个构建过程里**一个字节都不改**，所以检索 pin 的仍是旧版本
——不是「旧版本 + 降级标记」，是完全不变。本模块的每一条失败路径都不碰那一列。

## 失败只落 failed（决策三）

``error_message`` 只存**固定文案**：异常堆栈只进日志，绝不进库（失败留痕的同一条纪律，
见 ``app/tasks/indexing.py``）。``failed`` 的版本不会自己重试——重建请重新发起一次构建，
那是另一个版本号。

## 收尾用真值，不用计数器（#214）

**判据只有一条**（:func:`decide_convergence`）：这一版、这一稿下**已经有切片的文档数**
（:func:`app.repositories.chunk_repository.count_documents_in_version`），与构建开始时快照
的参与篇数比较。够了、且没有失败明细才允许 ``ready``；不够就是 ``failed``。

为什么这件事值得单独写一段：在这之前，判据是一个**假计数**——构建循环丢掉了每篇重建函数
的返回值，抢不到也算一篇「已重建」，而四项计数对账在「切片 0 / 向量 0 / 关键词 0」时三方
相等，于是**一篇都没真正重建的构建也能 ready 并激活**，线上静默变成「版本正常、检索全
空」。计数器漂高就会这样；真值不会：它任何时刻重算一次都得到同一个数，丢一条通知最多让
收尾**晚一点**（[ADR-0034](../../docs/adr/0034-queue-state-and-build-convergence.md) 决策四）。

三件事因此分开，**不许互相顶替**：

| 动作 | 回答的问题 | 实现在哪 |
| --- | --- | --- |
| :meth:`IndexBuildService.collect_truth` | 现在真值是多少（读库） | 切片表 ``COUNT(DISTINCT document_id)`` + 构建行上的两个快照列 |
| :func:`decide_convergence` | 该收了没有（纯函数） | 四个输入：真值 / 参与篇数 / 失败明细 / 是否最后一次 |
| :meth:`IndexBuildService.converge` | 收（迁移状态，幂等） | 真值够走 :meth:`~IndexBuildService.mark_ready`（仍过四项计数），不够走 :meth:`~IndexBuildService.mark_failed` |

``ready`` 依然要过 [ADR-0025](0025-index-as-build-artifact.md) 的四项计数对账：真值回答
「该收了吗」，四项计数回答「这版能上线吗」，两个判据各自说话。

## 取消与进程死掉的差别（刻意不对称）

* **任务内的异常**（含 Celery 的 ``SoftTimeLimitExceeded``）→ :meth:`IndexBuildService.build`
  把它收成 ``failed`` + 原因；
* **进程被 SIGKILL / 被取消** → 那一行**留在 building**，因为「写一笔可提交的事务」这件事
  在被取消的上下文里本身就不可靠。留着的 ``building`` 行不是半成品上线（线上照旧查旧的
  active），而是运维可见的一个待裁决项：:meth:`IndexBuildService.list_stalled_builds`
  按年龄把它指出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Callable, Sequence
from typing import Any, Final

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.base import now
from app.db.transaction import async_commit_now, async_rollback_now, async_transaction
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    INDEX_BUILD_BUILDING,
    INDEX_BUILD_FAILED,
    INDEX_BUILD_READY,
    IndexBuildVersion,
    effective_index_version,
    next_version_name,
)
from app.providers.keyword_search import KeywordSearchProvider, get_keyword_search_provider
from app.providers.vectorstores.base import VectorStore
from app.providers.vectorstores import get_vector_store
from app.models.knowledge_base import KnowledgeBase, build_params_of
from app.repositories import (
    chunk_repository,
    document_repository,
    index_version_repository,
    knowledge_base_repository,
)
from app.services.index_dispatch import enqueue_build_document
from app.services.indexing_service import IndexingService
from app.utils.id_generator import new_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

#: ``failed`` 时落库的唯一文案（一因一码：失败原因分几类就几个常量，不拼异常文本）。
BUILD_FAILED_MESSAGE: Final[str] = "index build failed, see worker logs"
#: 另一个构建还在跑时拒绝发起新构建的话术。
BUILD_ALREADY_RUNNING_MESSAGE: Final[str] = (
    "an index build is already running for this knowledge base"
)
#: 状态迁移不合法（例如没 ready 就想 active）时的固定话术。
BUILD_INVALID_TRANSITION_MESSAGE: Final[str] = "index build status transition is not allowed"
#: 回滚的目标版本**已经没有可查的数据**（物理索引不在 / 切片为 0）时的话术。
ROLLBACK_DATA_MISSING_MESSAGE: Final[str] = (
    "index version has no physical data left; refusing to roll back"
)
#: 两条入口（激活与回滚）撞上同一次并发切换时的话术。
SWITCH_CONFLICT_MESSAGE: Final[str] = (
    "another index version switch won the race for this knowledge base"
)
#: 四项计数不相等时拒绝 ``ready`` 的话术（半成品不许上线）。
BUILD_COUNTS_MISMATCH_MESSAGE: Final[str] = (
    "index build counts do not match; refusing to mark it ready"
)
#: 库里有文档、这一轮却一篇都没重建成功时的话术（第 16 步 #207）。
#:
#: 为什么要把这件事当失败而不是「建成一个空版本」：空版本的四项计数**自洽**（0=0=0=0），
#: 所以它能一路通过计数闸门变成 ``ready``——而它一旦被激活，检索会诚实地返回空结果，
#: 读起来却像「这个库没内容」。症状不报错、只是全空，正是这个仓库最不愿意留下的那类状态。
BUILD_NOTHING_TO_REBUILD_MESSAGE: Final[str] = (
    "no document could be rebuilt; refusing to build an empty index version"
)
#: 真值不够、而这一轮不会有人再补时的话术（#214）。
#:
#: 与上面那条的分工：那条说的是「一篇都没进来」（库里有文档、真值 0），这条说的是
#: 「进来了一部分、但没盖满快照的参与篇数」。两者对运维的指向不同——前者去查「为什么这批
#: 文档全都没被重建」，后者去查「差的这几篇卡在哪」——所以不合并成一句。
BUILD_INCOMPLETE_MESSAGE: Final[str] = (
    "index build did not cover every planned document; refusing to mark it ready"
)
#: 一行 ``building`` 停留超过这个时长就当「有行、没任务」（运维可据此重跑或标失败）。
BUILD_STALL_SECONDS: Final[int] = 60 * 60

#: 收敛判定里的「还没到时候」（不是状态，是一个**不迁移**的结论）。
CONVERGE_WAIT: Final[str] = "wait"

#: 收敛期限的基数（秒）：即使只有一篇，也要给「投递 → 起任务 → 跑完」留出余量。
CONVERGE_BASE_SECONDS: Final[int] = 15 * 60
#: 每篇文档的收敛预算（秒）。
#:
#: **口径**：这是「整库跑完」的预算，不是「单篇」的预算——这一票的投递形状仍是**一个任务
#: 跑完整个库**（按篇投递是 #215），所以期限必须覆盖这一整条循环。每篇 8 秒取的是保守
#: 上界（嵌入按批走，单篇解析 + 切块 + 向量化通常几秒；大批量文档偏慢），宁可给宽一点：
#: 期限判早了，代价是把一次**正常**的构建收成 ``failed``（ADR-0034 决策五的翻案条件里
#: 明确把它当成一次误判）。#215 按篇投递之后每篇自带租约，这个公式应当随之收紧。
CONVERGE_PER_DOCUMENT_SECONDS: Final[int] = 8
#: 收敛期限的封顶（秒）。
#:
#: 6 小时：再大的库，也轮不到一个「跑完整个库」的任务在单进程里跑一整天——那已经不是慢，
#: 是任务根本没被跑（进程死了 / 队列堵了），而这种情形该由清扫器发现，不该由期限假装能等。
CONVERGE_MAX_SECONDS: Final[int] = 6 * 60 * 60

#: 派活任务一次投多少篇（#215）。
#:
#: 一批几百条是 Broker 与 Worker 内存都舒服的量级。一次把 5000 条挤进 Redis 会让「构建」那条
#: 队列一瞬间比所有租户的上传加起来还长——而队列长度本身就是运维要看的信号，淹掉它就等于
#: 把这个信号弄坏。
DISPATCH_BATCH_SIZE: Final[int] = 200

#: 清扫器最多重投几次同一构建（ADR-0034 决策七）。
#:
#: 兜底必须有个尽头：一个永远救不回来的构建（例如某篇源文件坏了）会被清扫器每轮数出同一个
#: 缺口、投同一批任务，队列里因此常年躺着一批必然失败的消息。到顶就收成 ``failed``——
#: 「救不回来」也是一个必须兑现的结论。
MAX_REDISPATCH: Final[int] = 3

#: 「未入新版本」明细里单条原因的固定文案（不拼异常原文，与失败留痕同一条纪律）。
BUILD_DOCUMENT_FAILED_REASON: Final[str] = "document indexing failed"

#: 合法迁移表。空集合 = 终态，谁也别想再动它。
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    INDEX_BUILD_BUILDING: frozenset({INDEX_BUILD_READY, INDEX_BUILD_FAILED}),
    INDEX_BUILD_READY: frozenset({INDEX_BUILD_ACTIVE, INDEX_BUILD_FAILED}),
    INDEX_BUILD_ACTIVE: frozenset({INDEX_BUILD_ARCHIVED}),
    # 回滚（#155）：归档**不是**终态。「新切法出问题就退回去」是 ADR-0025 承诺的四样收益
    # 之一；把它写成空集合，保留 v1 就只服务了评测，没服务线上。
    INDEX_BUILD_ARCHIVED: frozenset({INDEX_BUILD_ACTIVE}),
    INDEX_BUILD_FAILED: frozenset(),
}


class IndexBuildAlreadyRunning(AppError):
    """该库已经有一条 ``building`` 行：第二次构建在开始处就被拒。

    错误码用的是既有的 ``SYSTEM_BUSY``（50001），**没有新增码**：错误码全表是契约，
    为一条内部编排路径加码要连带改文档与门禁，而那属于「新增入口」的票。调用方要区分
    的话，本类本身就是那个区分点。
    """

    def __init__(self, running: IndexBuildVersion | None = None) -> None:
        self.running = running
        super().__init__(ErrorCode.SYSTEM_BUSY, BUILD_ALREADY_RUNNING_MESSAGE)


class IndexBuildInvalidTransition(AppError):
    """不合法的一次状态迁移（例如 ``building`` 直接跳 ``active``）。"""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(ErrorCode.SERVER_ERROR, BUILD_INVALID_TRANSITION_MESSAGE)


class IndexBuildRollbackRefused(AppError):
    """回滚的目标版本**没有可查的数据**：拒绝上线。

    这是本票最容易做错的那条守卫（#155）：库列一改、别名一切，检索就按目标版本过滤，
    而那个版本的数据已经不在了——于是每一次检索都是一次「诚实的空结果」，被包装成
    「本次没命中」。宁可在这里显式拒绝，也不要让运维在几天后从「怎么搜不到了」开始查。

    ``keyword_index`` 为 ``None`` 表示关键词能力不存在（没部署 ES）——这一维不参与判定
    （与 #142 的四项计数同一口径：把「没有这项能力」当成 0 会把能力缺失伪装成数据不一致）。
    """

    def __init__(self, *, version: str, keyword_index: bool | None, chunks: int) -> None:
        self.version = version
        self.keyword_index = keyword_index
        self.chunks = chunks
        super().__init__(ErrorCode.SERVER_ERROR, ROLLBACK_DATA_MISSING_MESSAGE)


class IndexBuildSwitchConflict(AppError):
    """两条切换撞在一起，这条输了（部分唯一索引把它挡在提交处）。

    **两条入口抛同一个类**：激活与回滚都是「把某个版本变成 active」，撞上并发时调用方
    该看到的是同一句话，而不是「回滚有一句错误、激活另有一句」。

    错误码沿用既有的 ``SYSTEM_BUSY``（50001），**没有新增码**——理由与
    :class:`IndexBuildAlreadyRunning` 相同：错误码全表是契约，为一条内部编排路径加码要
    连带改文档与门禁，那属于「新增入口」的票。
    """

    def __init__(self) -> None:
        super().__init__(ErrorCode.SYSTEM_BUSY, SWITCH_CONFLICT_MESSAGE)


@dataclass(frozen=True)
class BuildCounts:
    """四项计数的**读数快照**（ADR-0025 决策一：相等才允许 ``ready``）。

    | 维度 | 从哪来 |
    | --- | --- |
    | ``knowledge_bases`` | 本次构建的作用域，恰好一个库（库行读得到才是 1） |
    | ``chunks`` | ``chunks`` 表里 ``(租户, 库, 目标版本)`` 的行数 |
    | ``vectors`` | 向量存储里同一范围内的可检索条数 |
    | ``keyword_documents`` | 关键词引擎里目标版本的物理索引中的文档数 |

    **「相等」只能是这个意思**：知识库数是**范围计数**（一个库 = 1），另外三项是数据
    计量（每库 N 片）。字面意义的「四个数字彼此相等」在 N>1 时永远不成立，所以判据是
    「范围计数 == 1，且三项数据计数彼此相等」——ADR 想挡的是「切片的数量与向量 / 关键词
    的数量对不上」，那种版本上线后的表现是「有些文档搜不到」，且不报错。

    ``keyword_documents`` 为 ``None`` 表示**关键词能力不存在**（没部署 ES，Provider 是空
    实现）：这一维不参与判定。否则没有 ES 的部署永远到不了 ``ready``——把「没有这项能力」
    当成「这一维是 0」，会把能力缺失伪装成数据不一致。
    """

    knowledge_bases: int
    chunks: int
    vectors: int
    keyword_documents: int | None

    @property
    def matched(self) -> bool:
        """四项计数是否对得上（口径见类文档）。"""

        if self.knowledge_bases != 1:
            return False
        if self.chunks != self.vectors:
            return False
        return self.keyword_documents is None or self.keyword_documents == self.chunks

    def as_dict(self) -> dict[str, int | None]:
        """落日志用的扁平形状（`关键词文档数` 可能是 ``None``）。"""

        return {
            "knowledge_bases": self.knowledge_bases,
            "chunks": self.chunks,
            "vectors": self.vectors,
            "keyword_documents": self.keyword_documents,
        }


class IndexBuildCountsMismatch(AppError):
    """四项计数不相等：**拒绝**标记 ready（半成品上线后表现为「有些文档搜不到」）。"""

    def __init__(self, counts: BuildCounts) -> None:
        self.counts = counts
        super().__init__(ErrorCode.SERVER_ERROR, BUILD_COUNTS_MISMATCH_MESSAGE)


class IndexBuildIncomplete(AppError):
    """真值不够、而这一轮不会有人再补：这次构建收成 ``failed``。

    与 :class:`IndexBuildNothingToRebuild` 分两个类，是因为运维要查的是两件事：那条说
    「一篇都没进来」，这一条说「进来了一部分、差的还在别处」。文案也各有一句
    （:data:`BUILD_INCOMPLETE_MESSAGE` / :data:`BUILD_NOTHING_TO_REBUILD_MESSAGE`）。
    """

    def __init__(self) -> None:
        super().__init__(ErrorCode.SERVER_ERROR, BUILD_INCOMPLETE_MESSAGE)


class IndexBuildNothingToRebuild(AppError):
    """库里有文档，这一轮却一篇都没重建成功。

    绝大多数情形是「那些文档卡在 ``processing``」——上传或重建的 Worker 死在半路，
    认领时写的 ``processing`` 就再也没被改回来。它不是构建自己的错，但**不能假装
    建成了一个空版本**：那会被激活、会返回空结果，而症状看起来像「这个库没内容」。

    #214 起这条守卫的判据换成了真值（:func:`decide_convergence`）：真值为 0、而参与篇数
    快照大于 0 → 必然 ``failed``。它不再依赖任何计数器——空版本上线的那条路是从判据上拆掉的，
    不是在判据之后又拦一道。
    """

    def __init__(self) -> None:
        super().__init__(ErrorCode.SERVER_ERROR, BUILD_NOTHING_TO_REBUILD_MESSAGE)


@dataclass(frozen=True)
class BuildTruth:
    """收敛判据的一次读数快照（#214）：真值 / 分母 / 稿号。

    三样东西一起返回，因为它们必须来自**同一次**判断：只把真值传给调用方，它就会自己去取
    分母，而两次读之间的窗口里分母会变（那正是「快照 vs 现数」要分开的原因）。

    ``documents`` 与 ``planned`` 的差就是「还缺几篇」；``as_dict`` 供日志与运维脚本打印。
    """

    #: 真值：这一版、这一稿下已经有切片的**文档数**。
    documents: int
    #: 参与篇数快照：起构建那一刻该库可参与构建的文档数。
    planned: int
    #: 判据用的稿号；``None`` = 没有稿号快照（老行），此时真值不按稿过滤。
    doc_version: int | None = None
    #: 库里**实际有多少文档**（不论状态）。只用于区分「真空库」与「有文档但一篇都不合格」：
    #: 后者不许产出 ready 空版本，也不该用「去看 worker 日志」这句文案（那时没有 worker 参与）。
    #: 默认 0 让老调用方（只喂三个字段的地方）不必改。
    documents_in_kb: int = 0

    @property
    def shortfall(self) -> int:
        """还缺几篇（负数按 0：真值超过快照是「文档变多了」，不是欠账）。"""

        return max(self.planned - self.documents, 0)

    def as_dict(self) -> dict[str, int | None]:
        """落日志用的扁平形状。"""

        return {
            "documents": self.documents,
            "planned": self.planned,
            "doc_version": self.doc_version,
            "shortfall": self.shortfall,
        }


def can_transition(current: str, target: str) -> bool:
    """``current`` 能否直接迁到 ``target``（纯函数，状态机的真值表只有这一份）。"""

    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


#: 「失败原因文案 ⇄ 异常类」的唯一一张表：:func:`_failure_reason_of` 按数字选文案，
#: ``IndexBuildService._aborted`` 按文案选异常类。两个方向共用一个映射。
#: 值的类型收窄成这两个**无参可构造**的子类：写成 ``type[AppError]`` 会让 mypy 认为
#: 构造需要 ``code``（AppError 的必填位），于是 ``error_type()`` 一行报 no-any-return。
_FAILURE_BY_REASON: dict[
    str, type[IndexBuildNothingToRebuild] | type[IndexBuildIncomplete]
] = {
    BUILD_NOTHING_TO_REBUILD_MESSAGE: IndexBuildNothingToRebuild,
    BUILD_INCOMPLETE_MESSAGE: IndexBuildIncomplete,
    BUILD_FAILED_MESSAGE: IndexBuildIncomplete,
}


def _failure_reason_of(truth: BuildTruth) -> str:
    """按真值选**失败原因文案**（:data:`_FAILURE_BY_REASON` 的正向）。

    三档的区分是为了让排查从「哪种失败」开始，而不是从「构建失败了」开始：

    * 参与篇数 > 0 而真值 0 —— 一篇都没进来（多半是版本 / 稿号对不上，或全都抢不到租约）；
    * 真值 < 参与篇数 —— 进来了一部分但没盖满；
    * 其余（真值够但有明细）—— 建满了、但有几篇没进来，运维去查 ``failed_documents``。

    文案本身**不拼异常原文**（脱敏纪律与单篇失败一致）。
    """

    # 第一档看两个数：参与篇数 > 0（老口径），**或**库里实际有文档（守卫新增的那一格）。
    # 只看 ``planned`` 会让「库里有十篇、但没有一篇可参与」（planned=0）掉进第三档，
    # 落成「去看 worker 日志」——而那时一篇子任务都没投出去，日志里什么都不会有。
    if truth.documents == 0 and (truth.planned > 0 or truth.documents_in_kb > 0):
        return BUILD_NOTHING_TO_REBUILD_MESSAGE
    if truth.documents < truth.planned:
        return BUILD_INCOMPLETE_MESSAGE
    return BUILD_FAILED_MESSAGE


def converge_deadline_for(planned: int, *, started_at: datetime) -> datetime:
    """起构建那一刻算出这次构建的**收敛期限**（纯函数，ADR-0034 决策五）。

    公式：``开始时刻 + 基数 + 每篇预算 × 参与篇数``，封顶 :data:`CONVERGE_MAX_SECONDS`。

    三个常数各自的依据写在上面的常量注释里，这里只再强调一件事：**它是「最晚给我一个
    结论」的时间点，不是「第几篇跑完」的时间点**。所以它与库规模相关（篇数越多越晚），
    而与「现在跑到第几篇」无关——后者是进度，进度不做判据（同一个理由见
    :func:`decide_convergence`）。

    ``planned`` 为负按 0 算：调用方给负数说明它读错了库，而期限是**兜底**，兜底不该因为
    一个坏输入变成一个立刻过期的死线。
    """

    documents = max(planned, 0)
    budget = CONVERGE_BASE_SECONDS + CONVERGE_PER_DOCUMENT_SECONDS * documents
    return started_at + timedelta(seconds=min(budget, CONVERGE_MAX_SECONDS))


def decide_convergence(*, truth: int, planned: int, failed: int, final: bool = True) -> str:
    """这一次构建**该不该收尾**：返回 ``ready`` / ``failed`` / :data:`CONVERGE_WAIT`。

    纯函数，三个数字（加一个「是不是最后一次」）进去，一个结论出来——于是四种组合可以在
    离线用例里逐条钉死，而不必去连库、造切片、摆时间。

    | 真值 | 失败明细 | ``final`` | 结论 | 为什么 |
    | --- | --- | --- | --- | --- |
    | 够（≥ 参与篇数） | 无 | 任意 | ``ready`` | 该建的都建了 |
    | 够 | 有 | 任意 | ``failed`` | 部分版本**永不上线**（ADR-0025）；有失败明细就不许它上线 |
    | 不够 | 有 | 任意 | ``failed`` | 同上，且真值也不够 |
    | 不够 | 无 | ``True`` | ``failed`` | 这一轮不会有人再补了（最后一篇跑完 / 期限已过） |
    | 不够 | 无 | ``False`` | ``wait`` | 还可能有篇在飞：此时判死会把一次正常构建打回去 |

    **``final`` 是什么**：调用方对「还有没有人会往这一版里写」的断言。一个任务跑完整个库的
    形状（这一票）在循环结束时它恒为 ``True``；按篇投递与清扫器（#215 / #224）在期限之前
    给 ``False``，让「还差几篇」如实表达成「再等等」。它是**参数**而不是时钟：判据里不引入
    第二份时间真相，时间只从构建行的收敛期限来。

    **``truth == planned == 0`` 时回 ``ready``**：那个库起构建时就没有可参与的文档，建一个
    空版本是诚实的结果（老实现的同一口径）。「一篇都没重建成功」的守卫说的是 ``planned > 0``
    而 ``truth == 0``——那才是「库里明明有文档，却一片都没进来」。
    """

    if failed > 0:
        return INDEX_BUILD_FAILED
    if truth >= planned:
        return INDEX_BUILD_READY
    return INDEX_BUILD_FAILED if final else CONVERGE_WAIT


def _nothing_to_rebuild_message(*, total: int, pending: int) -> str:
    """「起不了构建」的**下一步**文案：按文档状态说清该做什么。

    两种情形指向的动作不同，混成一句会让人做错事：

    * 有篇在排队 / 正在索引 → 该做的是**等它跑完**（这时点「重建」只会撞 20025「正在索引」）；
    * 全都是失败 → 该做的是点「重建」或重新上传。

    ``pending`` 是「排队中 + 索引中」的篇数，``total`` 是该库文档总数。
    """

    if pending > 0:
        return (
            f"这个库里有 {total} 篇文档，其中 {pending} 篇还在排队或正在索引——"
            "构建只重建「成功」的文档。等它们跑完（或失败后点「重建」）再改设置。"
        )
    return (
        f"这个库里有 {total} 篇文档，但没有一篇处于「成功」状态——"
        "构建只重建成功的文档。先对失败的文档点「重建」或重新上传，再改设置。"
    )


def guard_against_an_empty_version(verdict: str, *, planned: int, documents_in_kb: int) -> str:
    """把「0 篇参与、0 篇建成」的 ``ready`` 判成 ``failed``——**当库里还有文档时**。

    ## 为什么这不是 ``decide_convergence`` 的一部分

    ``decide_convergence`` 是**纯函数**：它只看真值、计划数与失败明细，不读库。而这条守卫需要
    回答「库里到底有没有文档」——那是另一次读。混进纯函数会让它不再纯，也就不再能用四条真值
    组合把它钉死。

    但它必须**只有一处**：任务体（``converge``）、运维脚本（``reconcile_build.py``）、清扫器
    都算这一句话。否则会出现「脚本说该 ready、服务写下去却是 failed」这种两个真相并存。

    ## 三种情况的区分

    * 空库（``documents_in_kb == 0``）：``ready`` 是对的——空库产出空版本是 ADR-0025 允许的，
      它本来就检索不到东西。
    * 有文档、但一篇都不可参与（全失败 / 全排队中）：``failed``——若激活这个空版本，线上检索
      会一条都查不到，而界面上写着「构建成功」。
    * ``planned > 0``：守卫不参与，按 ``decide_convergence`` 的判决走。
    """

    if verdict != INDEX_BUILD_READY or planned != 0:
        return verdict
    return INDEX_BUILD_FAILED if documents_in_kb > 0 else verdict


def ensure_transition(row: IndexBuildVersion, target: str) -> None:
    """迁移不合法就抛 :class:`IndexBuildInvalidTransition`，合法则什么都不做。"""

    if not can_transition(row.status, target):
        raise IndexBuildInvalidTransition(row.status, target)


def ensure_activation_origin(row: IndexBuildVersion, *, origin_status: str) -> None:
    """这一行能不能从 ``origin_status`` 出发变成 ``active``。

    两件事各司其职：真值表（:data:`ALLOWED_TRANSITIONS`）回答「这个目标状态合不合法」，
    ``origin_status`` 回答「这条入口认不认它当起点」。分开之后，激活（``ready``）与回滚
    （``archived``）不会互相冒充——尤其不会让 ``activate`` 顺路激活一个归档版本，
    从而绕过回滚特有的物理数据守卫。
    """

    ensure_transition(row, INDEX_BUILD_ACTIVE)
    if row.status != origin_status:
        raise IndexBuildInvalidTransition(row.status, INDEX_BUILD_ACTIVE)


def _is_failure_allowed(row: IndexBuildVersion) -> bool:
    """这条行还允许被标记失败吗。

    ``failed`` 只从 ``building`` / ``ready`` 来。``active`` / ``archived`` 收到一次迟到
    的失败上报时**什么都不改**（只记一条 warning）：生效版本不该被一次收尾覆盖，否则
    线上会在一瞬间「没有生效版本」——那正是 ADR-0025 决策三要防的那种中间态。
    """

    return can_transition(row.status, INDEX_BUILD_FAILED)


class IndexBuildService:
    """一次构建的编排：起版本行、逐文档重建、校验、激活。

    **依赖全部从构造参数注入**（与 ``IndexingService`` 同一条纪律）：离线用例注入
    记录型替身即可跑完整条编排，不连 PostgreSQL、不连 ES、不发网络。
    """

    def __init__(
        self,
        *,
        indexing: IndexingService | None = None,
        vector_store: VectorStore | None = None,
        keyword_search: KeywordSearchProvider | None = None,
    ) -> None:
        self._indexing = indexing if indexing is not None else IndexingService()
        self._vector_store = vector_store if vector_store is not None else get_vector_store()
        self._keyword_search = (
            keyword_search if keyword_search is not None else get_keyword_search_provider()
        )

    # ── 起一个构建 ────────────────────────────────────────────────

    async def start_build(
        self,
        db: AsyncSession,
        *,
        kb_id: str,
        tenant_id: str,
        version: str | None = None,
        auto_activate: bool = False,
    ) -> IndexBuildVersion:
        """为这个库建一行 ``building``，返回它（版本名缺省由已有版本算出）。

        **并发只允许一个**：先读一次 ``building`` 行把拒绝说得清楚，再靠那条部分唯一索引
        兜底——读与写之间的窗口里另一个请求可能刚插进去，那时 ``flush`` 的
        ``IntegrityError`` 会被翻译成同一个 :class:`IndexBuildAlreadyRunning`。只做前者是
        假守卫（TOCTOU），只做后者则拿不到「已经在建的是哪一行」这个可读的信息。
        """

        kb = await knowledge_base_repository.get_by_id_for_tenant(db, kb_id, tenant_id=tenant_id)
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        existing = await index_version_repository.list_for_kb(db, kb_id, tenant_id=tenant_id)
        running = next((row for row in existing if row.status == INDEX_BUILD_BUILDING), None)
        if running is not None:
            # ── 存量卡住的行：先就地收敛，再判断要不要拒绝 ──
            #
            # 现场 #226 的第二半：一条 `planned = 0` 的 `building` 行（起它的时候库里一篇
            # 可参与的文档都没有）会永远占着这个位置——并发守卫让用户「再也改不了设置」，
            # 而清扫器不一定在跑（`RATE_LIMIT_ENABLED=off` 的部署里它根本不启动）。
            # 所以入口自己先给它一个结论：这种行**必然**收敛成 failed（守卫的判据），收敛
            # 之后 `running` 就空了，用户这一次保存能直接继续。
            if running.documents_planned == 0:
                await self.converge(db, str(running.id), tenant_id=tenant_id, final=True)
                running = None
            else:
                raise IndexBuildAlreadyRunning(running)

        # 建库只写了一列、没写行（#141 的落法）：激活时要能归档「上一版」，就必须先把
        # 当前生效版本补成一行。缺了它，第一轮构建的激活会「找不到旧 active 可归档」，
        # 而库列被改掉之后旧版本的数据就永远停在「无人认领」的状态。
        adopted = await self._adopt_live_version(db, kb, existing)
        names = [row.version for row in existing] + ([adopted] if adopted else [])

        # 起构建这一刻的**两个快照**（#214）：参与篇数与统一稿号。收敛时真值就是拿它们
        # 当分母与筛子比的，所以它们必须写在开跑之前——构建跑起来之后这个库还会变
        # （有人上传、有人删），而「这一轮该建几篇」不该跟着变。
        planned, draft = await document_repository.participating_snapshot(
            db, kb_id, tenant_id=tenant_id
        )
        # ── 「有文档、但一篇都不可参与」不是「空库构建」──
        #
        # 这个库当前可能一个文档都没有（合法空库，产出空版本是 ADR-0025 允许的），也可能
        # **有十篇、但全是失败 / 排队中**（构建只重建 `1 成功` 的文档）。后者的正确处置是
        # 立刻告诉用户「先重建那些失败的篇」，而不是起一个 0 篇的构建：
        #
        # 那种构建会一路走到 `ready` 并**激活一个空版本**——线上检索从此一条都查不到，而
        # 界面上「构建成功」。这正是本步反复要消灭的形态（「版本正常、检索全空」）。
        # 它还会在收敛之前一直占着 `building` 行，于是 `start_build` 的并发守卫让用户
        # **再也改不了这个库的设置**（症状是「提示库在重建中，但进度永远不动」）。
        if planned == 0:
            total = await document_repository.count_by_kb(db, kb_id, tenant_id=tenant_id)
            if total > 0:
                # 按状态分支给下一步动作：全都当「失败的文档」会让刚上传的用户去点重建，
                # 而它们其实在排队或正在索引——那种情形该做的是**等**。
                pending = await document_repository.count_by_kb_in_states(
                    db,
                    kb_id,
                    tenant_id=tenant_id,
                    statuses=(DocumentStatus.QUEUED, DocumentStatus.PROCESSING),
                )
                raise AppError(
                    ErrorCode.KB_NOTHING_TO_REBUILD,
                    _nothing_to_rebuild_message(total=total, pending=pending),
                )
        started_at = now()

        row = IndexBuildVersion(
            id=new_id(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            version=version or next_version_name(names),
            status=INDEX_BUILD_BUILDING,
            # 这一版是**用哪些切块参数**构出来的（第 16 步 #207）。记在开跑那一刻：
            # 构建期间有人改库设置，这一版仍然属于旧参数——事后复盘「为什么这版还是整节」
            # 靠的就是这一格，而不是去猜当时的设置。
            params=build_params_of(kb),
            created_at=started_at,
            # 参与篇数快照：收敛判据的分母（真值 = 该版本该稿下已有切片的文档数）。
            documents_planned=planned,
            # 统一稿号：库里没有可参与的文档时记 1，因为这一列非空，而「没有稿号」这件事
            # 已经由 ``documents_planned == 0`` 表达（分母为零，真值怎么数都够）。
            doc_version=draft if draft is not None else 1,
            # 进度从零开始，**只做进度条**（判定读真值，不读它）。
            documents_done=0,
            # 收敛期限 = 构建行自己的租约：最晚什么时候必须有人给它一个结论（决策五）。
            converge_deadline=converge_deadline_for(planned, started_at=started_at),
            # 「改切块设置」那条路径置真：这一版收敛成 ready 后就该立刻生效（迁移
            # ``f1a4b8d6c392``）。其余路径一律 false——常规重建停在 ready 等运维点激活，
            # 那是 ADR-0025 决策三明确要留的一步。
            auto_activate=auto_activate,
        )
        try:
            await index_version_repository.create(db, row)
        except IntegrityError as exc:
            # 另一个请求在「读过之后、插之前」抢先占了 building 位：回滚掉这笔半成品，
            # 抛同一个业务异常（调用方看到的两种拒绝必须一样）。
            await async_rollback_now(db)
            raise IndexBuildAlreadyRunning(None) from exc
        await async_commit_now(db)

        logger.info(
            "index build started",
            build_id=row.id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            version=row.version,
        )
        return row

    async def _adopt_live_version(
        self, db: AsyncSession, kb: KnowledgeBase, rows: Sequence[IndexBuildVersion]
    ) -> str | None:
        """把库里「当前生效版本」补成一行 ``active``；补了就返回那个版本名。

        ``rows`` 是调用方刚读到的版本行（不再查一次：同一条语句读两遍只会在两次读之间
        留下一个新的窗口）。
        """

        live = effective_index_version(kb)
        if live is None:
            return None
        if any(row.version == live for row in rows):
            return None
        if any(row.status == INDEX_BUILD_ACTIVE for row in rows):
            # 库里已经有一行 active，却与库列指向的版本名不一致：这是**人手改库**
            # 才会出现的状态，不自作主张再插一行（会撞部分唯一索引），只记一条 warning。
            logger.warning(
                "effective index version disagrees with the build table",
                kb_id=kb.id,
                tenant_id=kb.tenant_id,
                effective_version=live,
            )
            return None
        await index_version_repository.create(
            db,
            IndexBuildVersion(
                id=new_id(),
                tenant_id=str(kb.tenant_id),
                kb_id=str(kb.id),
                version=live,
                status=INDEX_BUILD_ACTIVE,
                created_at=now(),
                activated_at=now(),
            ),
        )
        return live

    # ── 跑一次构建 ────────────────────────────────────────────────

    async def build(self, db: AsyncSession, build_id: str, *, tenant_id: str) -> IndexBuildVersion:
        """跑完一次构建：逐文档重建 → **真值收敛**（该 ready 就 ready，否则 failed）。

        激活**不**在这里做：它要改线上（库上的生效版本列），必须是一个能被单独调起、
        单独重试、单独审计的动作。把两者塞进一个函数，运维就没有「先 ready、观察一会儿
        再切」的余地，而 ADR-0025 把 ``ready`` 明确写成了**运维可见的一步**。

        **一次构建 = 若干次「一篇文档一笔事务」**：整库包成一笔会把行锁持有到最后一篇
        （上传、删除、检索都要等它）；包成一笔也不换来什么，因为检索**不靠**构建的
        事务边界来保持一致——它 pin 在旧版本上（ADR-0025 决策二）。所以每一篇文档自己
        提交，失败的那一篇让整次构建落 ``failed``：部分版本永远不许上线。

        哪些文档参与：``SUCCESS`` 的那些。失败 / 处理中的文档没有可切的正文，把它们的
        失败再重放一遍只是把构建拖成失败；它们该由单篇重试解决，不属于构建。

        ## 为什么计数这件事变了（#214）

        ``index_existing_document`` 返回**写进去的切片数**，抢不到时返回 0。旧实现丢掉这个
        返回值、每篇都 ``indexed += 1``，于是「一篇都没真重建」也能通过唯一的空版本守卫，
        一路 ready 并激活（线上变成「版本正常、检索全空」）。现在：

        * ``documents_done`` 只数**真的写进切片**的篇数（返回值 > 0），抢不到的不计；
        * 它**只做进度条**，收不收尾由 :meth:`converge` 读真值决定。

        **它不再是生产路径**（#215 起构建是「派活 + 按篇投递」，由 `build_document_task`
        逐篇驱动）。留下它是因为「一次跑完整个库」作为**服务层能力**仍有用：
        `scripts/rebuild_kb_version.py --in-process` 与运维排障会直接调它——
        没有 Broker 也要能把一个库建出来。而它循环跑完就等于「这一轮不会有人再补了」，
        所以收敛时 ``final=True``。
        """

        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        if row.status != INDEX_BUILD_BUILDING:
            # 重复投递 / 迟到的任务：不是 building 就别再动它（尤其别动 active）。
            logger.warning(
                "index build is not building, skipping",
                build_id=build_id,
                tenant_id=tenant_id,
                status=row.status,
            )
            return row

        kb = await knowledge_base_repository.get_by_id_for_tenant(
            db, row.kb_id, tenant_id=tenant_id
        )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        try:
            documents = await document_repository.list_by_kb_async(
                db, row.kb_id, tenant_id=tenant_id
            )
            # 进度从零开始数**这一轮**真写进切片的篇数。写回行上前先置 0：一行自己带着
            # 上一轮的值（重投 / 同一行被跑第二遍）时，进度条不该从旧数字接着爬。
            indexed = 0
            row.documents_done = indexed
            for document in documents:
                if document.status != DocumentStatus.SUCCESS:
                    continue
                written = await self._indexing.index_existing_document(
                    db,
                    document.id,
                    # 目标版本是本票的唯一输入：写错版本等于「构建成功但没人看得见」。
                    index_version=row.version,
                    # 稿号用**构建行上的快照**（#215）：整库构建要跑很久，中途有人回滚某篇
                    # 文档不该让同一批切片一半是旧稿一半是新稿。这里改读快照而不是读每篇
                    # 自己的指针——按篇投递时子任务读的也是同一个快照，两条形状必须一致，
                    # 否则「一次任务跑完整个库」与「按篇投递」会产出两套不同的稿号归属。
                    doc_version=int(row.doc_version or 1),
                    # 重建的输入本来就是成功文档，必须允许抢占它们。
                    allow_success=True,
                )
                if written <= 0:
                    # 抢不到（另一个 Worker 正拿着这一篇）：**不计入**已重建。这条 if 就是
                    # 缺陷 2 的止血点——旧实现丢掉返回值，抢不到也计一篇，于是空版本能上线。
                    logger.info(
                        "index build skipped a document another worker owns",
                        build_id=build_id,
                        tenant_id=tenant_id,
                        document_id=document.id,
                    )
                    continue
                indexed += 1
                # 进度：写在这篇的提交**之后**，所以它落后「此刻真值」至多一篇——进度条
                # 晚一篇无伤，判据根本不用它（下一次提交会把最新值带上去）。
                row.documents_done = indexed

            # 收尾：真值够 → ready（仍过四项计数）；不够 → failed。两件事都在 converge 里，
            # 因为它要能被运维脚本 / 清扫器 / 最后一篇的持有者任意一方单独触发一次。
            converged = await self.converge(db, build_id, tenant_id=tenant_id, final=True)
        except Exception as exc:  # noqa: BLE001 - 收成 failed，原因只留固定文案
            await async_rollback_now(db)
            await self.settle_failure(db, build_id, tenant_id=tenant_id, cause=exc)
            logger.exception(
                "index build aborted",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=row.kb_id,
                version=row.version,
            )
            raise

        if converged.status == INDEX_BUILD_FAILED:
            # converge 已经把结论（failed + 固定文案）落库了；这里再抛一次，是让「构建没成」
            # 在任务日志与调用方那里都有痕迹——「静默失败」正是这次要堵的那类形态。
            raise self._aborted(converged)

        logger.info(
            "index build ready",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=converged.kb_id,
            version=converged.version,
            document_count=converged.document_count,
            rebuilt=indexed,
        )
        return converged

    async def _auto_activate(
        self, db: AsyncSession, ready: IndexBuildVersion, *, tenant_id: str
    ) -> IndexBuildVersion:
        """``auto_activate`` 为真的构建收敛成 ready 后立刻激活（#215 的回归修法）。

        ## 为什么失败只记 warning、把行留在 ready

        构建本身是好的（``ready`` + 四项计数已过），失败的是**切换**那一步（例如关键词别名
        切不动）。把刚建好的版本标成失败，运维重建一次也是白建；而 ``ready`` 是事实：版本可用，
        只是还没上线，可以重试激活。

        这与单篇索引的既有处置逐字一致——两条入口对同一件事给同一个
        答案，才不会出现「手动跑的会自动激活、面板点的不会」。

        ## 为什么用同一个会话、但要接住异常

        ``activate`` 自己 ``commit``：ready 那条结论在 ``mark_ready`` 里**已经提交过了**
        （所以这里出异常不会把「这一版 ready」一起回滚掉），而 ``activate`` 的内部实现是
        「别名 → 一笔事务（归档旧 active + 本行 active + 库列）」，失败时它自己回滚半成品。
        这里只负责**不让异常冒到收敛的调用方**：对调用方（一篇文章的构建子任务）来说，
        「这版已经 ready、只是没激活」是成功的，不是失败。
        """

        try:
            activated = await self.activate(db, ready.id, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 - 切换失败不能把「构建成功了」抹掉
            logger.warning(
                "index build auto-activation failed; leaving the build ready",
                build_id=ready.id,
                tenant_id=tenant_id,
                kb_id=str(ready.kb_id),
                version=str(ready.version),
                reason=type(exc).__name__,
            )
            logger.debug("auto-activation failure detail", exc_info=exc)
            return ready
        logger.info(
            "index build auto-activated",
            build_id=ready.id,
            tenant_id=tenant_id,
            kb_id=str(ready.kb_id),
            version=str(ready.version),
        )
        return activated

    @staticmethod
    def _aborted(converged: IndexBuildVersion) -> AppError:
        """把 converge 落下的结论翻回一个异常（任务日志与调用方看得见的那种）。

        ## 为什么只有一张表

        「失败原因文案 ⇄ 异常类」是**同一个映射的两个方向**：:func:`_failure_reason_of`
        按数字选文案，这里按文案选异常类。两套并列的 ``if`` 迟早会在某天给出两个答案
        （文案改了、异常没跟着改，症状是「一类失败被报成另一类」），所以它们共用
        :data:`_FAILURE_BY_REASON` 一张表。
        """

        for reason, error_type in _FAILURE_BY_REASON.items():
            if converged.error_message == reason:
                return error_type()
        # 兜底：不是收敛选出来的文案（例如运维手工改过 error_message）。归到「不完整」那一档
        # ——它比「一篇都没进来」更保守：不声称「库里是空的」。
        return IndexBuildIncomplete()

    # ── 收敛：真值 → 结论 → 一次状态迁移（#214）───────────────

    async def collect_truth(
        self, db: AsyncSession, *, row: IndexBuildVersion, tenant_id: str
    ) -> BuildTruth:
        """读一次收敛判据：**这一版、这一稿下已经有切片的文档数**，配上分母与稿号。

        真值来自 ``chunks`` 表（``COUNT(DISTINCT document_id)``），不是任何计数器：任何时刻
        重算都得到同一个数，丢的通知最多让收尾晚一点（ADR-0034 决策四）。

        **快照优先**：参与篇数与稿号都用构建行上起跑那一刻记下的值。没有快照的行（迁移之前
        起的构建、用例里手工摆的行）才回落到「按此刻库里的可参与文档读一份」——这一支必须
        存在，否则老行会带着 ``documents_planned = 0`` 走进判据，而分母为零意味着「真值 0
        也够」，恰好把这次要堵的那条空版本上线的路又留一条缝。
        """

        planned = row.documents_planned
        draft = row.doc_version
        if planned is None or draft is None:
            planned_now, draft_now = await document_repository.participating_snapshot(
                db, str(row.kb_id), tenant_id=tenant_id
            )
            planned = planned_now if planned is None else planned
            draft = draft_now if draft is None else draft
        documents = await chunk_repository.count_documents_in_version(
            db,
            str(row.kb_id),
            tenant_id=tenant_id,
            index_version=str(row.version),
            doc_version=draft,
        )
        # 顺手读一次「库里有几篇」：守卫（空库 vs 全失败）与失败文案（该不该提 worker 日志）
        # 都要这个数。放这里是**一次**读，而不是两处各读一次——两处读还可能读到不同的值。
        documents_in_kb = await document_repository.count_by_kb(
            db, str(row.kb_id), tenant_id=tenant_id
        )
        return BuildTruth(
            documents=documents,
            planned=int(planned or 0),
            doc_version=draft,
            documents_in_kb=documents_in_kb,
        )

    async def converge(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        final: bool = True,
    ) -> IndexBuildVersion:
        """给一次构建**下结论**：真值够就 ``ready``、不够就 ``failed``、还没到时候就等着。

        **幂等**：只有 ``building`` 的行会被推进，重复调用在第一步就原样返回。所以这个动作
        可以被任意一方触发——最后一篇跑完的那一方、清扫器、运维脚本——同时触发也不会收两次
        尾，更不会把一个已经 ``ready`` 的构建改回 ``failed``：状态迁移由真值表守门
        （:data:`ALLOWED_TRANSITIONS`），而这里连门都不推（``building`` 之外一概不动）。

        ``final`` 的语义见 :func:`decide_convergence`：一个任务跑完整个库时恒为 ``True``；
        按篇投递与清扫器在期限之前给 ``False``，让「还差几篇」如实表达成「再等等」。

        ``ready`` 仍然要过四项计数（:meth:`mark_ready` 里的那道闸门），**不许绕过**：
        真值回答「该收了吗」，四项计数回答「这版能上线吗」。
        """

        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        if row.status != INDEX_BUILD_BUILDING:
            logger.info(
                "index build already settled, converge skipped",
                build_id=build_id,
                tenant_id=tenant_id,
                status=row.status,
            )
            return row

        kb = await knowledge_base_repository.get_by_id_for_tenant(
            db, row.kb_id, tenant_id=tenant_id
        )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        truth = await self.collect_truth(db, row=row, tenant_id=tenant_id)
        # 失败明细是判据的第二个输入（#215）：真值只能回答「够不够」，回答不了「为什么不够」。
        # 有明细就不许上线——部分版本永不上线（ADR-0025），哪怕真值恰好被别的版本凑够了。
        failed_count = len(row.failed_documents or ())
        verdict = decide_convergence(
            truth=truth.documents,
            planned=truth.planned,
            failed=failed_count,
            final=final,
        )
        # ── 守卫：``planned == 0`` 但库里**有**文档 → 不许 ready ──
        #
        # 判据本身在 ``guard_against_an_empty_version``（纯函数，与 ``reconcile_build.py``
        # 共用同一处）：`start_build` 已经在起构建那一刻拒掉这种请求，这条守卫守的是**已经
        # 躺在库里的存量行**（升级前卡住的构建、以及清扫器按期限收尾那条路径）。
        if verdict == INDEX_BUILD_READY and truth.planned == 0:
            total = await document_repository.count_by_kb(db, str(row.kb_id), tenant_id=tenant_id)
            verdict = guard_against_an_empty_version(
                verdict, planned=truth.planned, documents_in_kb=total
            )
        if verdict == CONVERGE_WAIT:
            logger.info(
                "index build waiting for more documents",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=row.kb_id,
                version=row.version,
                **truth.as_dict(),
            )
            return row

        if verdict == INDEX_BUILD_READY:
            # 计数快照里的「篇数」用真值：它比「这一轮循环里数到了几篇」更接近事实
            # （续建、重投之后两者会不一样，而「这一版由几篇文档构成」问的是前者）。
            row.document_count = truth.documents
            counts = await self.collect_counts(db, kb=kb, version=row.version)
            ready = await self.mark_ready(db, build_id, tenant_id=tenant_id, counts=counts)
            # 「改切块设置」那条路径：收敛成 ready 之后就立刻激活（#215 的回归修法）。
            # 判断依据是**构建行上的这一格**，不是某个任务的载荷——清扫器、运维脚本收尾时
            # 读到的是同一个答案。
            if ready.status == INDEX_BUILD_READY and bool(ready.auto_activate):
                return await self._auto_activate(db, ready, tenant_id=tenant_id)
            return ready

        reason = _failure_reason_of(truth)
        # 明细存在但真值够：这是「建满了、但有篇没进来」——归到不完整那一档，运维去查
        # ``failed_documents`` 里那几条（文案本身不拼异常原文）。
        logger.warning(
            "index build cannot converge to ready",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            **truth.as_dict(),
        )
        failed = await self.mark_failed(db, build_id, tenant_id=tenant_id, reason=reason)
        return failed if failed is not None else row

    # ── 按篇投递（#215）──────────────────────────────────────────────

    async def list_resume_targets(
        self,
        db: AsyncSession,
        *,
        row: IndexBuildVersion,
        tenant_id: str,
        documents: Sequence[Any],
    ) -> list[Any]:
        """从参与构建的文档里挑出**还需要建**的那些（续建）。"""

        built = await chunk_repository.list_indexed_document_ids(
            db,
            str(row.kb_id),
            tenant_id=tenant_id,
            index_version=str(row.version),
            doc_version=int(row.doc_version or 1),
        )
        return [doc for doc in documents if doc.id not in built]

    async def dispatch(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        batch_size: int = DISPATCH_BATCH_SIZE,
        skip_existing: bool = True,
        publish: Callable[..., None] | None = None,
        settle_when_nothing_to_send: bool = False,
    ) -> int:
        """把这一版**该建的文档逐篇投进构建队列**，返回投了多少篇（#215）。

        ## 为什么按篇而不是「一个任务跑完整个库」

        一个任务跑完整个库有三个代价，都不是「调一下」能解决的：失败粒度是整个库（第 4999
        篇失败，前面 4998 篇的切片在 ``failed`` 版本下永远不激活，是孤儿数据）、进度不可见
        （只有开始与结束两条日志）、以及它整段时长只占一个 Worker 槽位——5000 篇的库能让
        别人的上传排队几十分钟。

        按篇之后每篇走的是**同一条流水线**（``IndexingService.index_existing_document``）：
        构建不是第二种索引，只是「带着 build_id 的一批单篇任务」。差异只有三处，全都由载荷
        带着走（目标版本 / 投递后是否进排队态 / 失败写哪里）。

        ## 续建

        ``skip_existing`` 让重投与清扫器只补缺的篇：目标版本 + 稿号下已有切片的直接跳过。
        重来一次不会再烧一整轮 embedding——这是按篇投递买到的最直接的收益。

        ## 分批

        一次 ``all()`` 读完再一口气投 5000 条会把 Broker 与本进程都顶住；这里按
        ``batch_size`` 投，投完一批再继续。**不做 sleep**：投递本身是 ``apply_async``（写
        Redis 一条命令），5000 条也就几秒；真需要背压时该由队列长度说话，不该在这里假装
        睡一会儿就能解决问题。
        """

        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        if row.status != INDEX_BUILD_BUILDING:
            logger.info(
                "index build is not building, dispatch skipped",
                build_id=build_id,
                tenant_id=tenant_id,
                status=row.status,
            )
            return 0

        documents = await document_repository.list_by_kb_async(db, row.kb_id, tenant_id=tenant_id)
        participating = [
            document for document in documents if document.status == DocumentStatus.SUCCESS
        ]
        if skip_existing:
            participating = await self.list_resume_targets(
                db, row=row, tenant_id=tenant_id, documents=participating
            )

        publisher = publish if publish is not None else enqueue_build_document
        dispatched = 0
        datestamp = now()
        for start in range(0, len(participating), batch_size):
            for document in participating[start : start + batch_size]:
                publisher(
                    document_id=document.id,
                    tenant_id=tenant_id,
                    kb_id=str(row.kb_id),
                    build_id=build_id,
                    index_version=str(row.version),
                    doc_version=int(row.doc_version or 1),
                    # 构建要重建的本来就是成功文档；``allow_success`` 是流水线那一侧的开关。
                    allow_success=True,
                    datestamp=datestamp,
                )
                dispatched += 1
        logger.info(
            "index build dispatched",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            planned=row.documents_planned,
            dispatched=dispatched,
            skipped=len(documents) - len(participating),
        )
        # ── 0 篇投出时可选择「当场给结论」——**只有派活任务能这么做** ──
        #
        # 现场 #226：库里十篇全失败 → 参与 0 → 派活投了 0 条就正常返回 → 没有任何一方收尾
        # → 构建行永远 `building` → 并发守卫让用户**再也改不了这个库的设置**。
        #
        # 但「0 篇」不能一律收尾：**清扫器的补投路径也会调用本方法**，而它在构建跑到最后
        # 一篇时必然看到「参与集为空」——因为正在跑的那一篇状态是 `PROCESSING`，不在
        # 「状态=成功」的参与集里。那一刻收尾就是 `decide_convergence(truth < planned,
        # final=True)` → **把健康的构建当场判 failed**（而子任务还在写切片）。
        #
        # 所以这个动作是**调用方显式要的**：派活任务要（它知道自己是这一轮的唯一入口）；
        # 清扫器不要（它只是按 60 秒节拍补缺，收尾归期限那一条路）。
        if dispatched == 0 and settle_when_nothing_to_send:
            await self.converge(db, build_id, tenant_id=tenant_id, final=True)
        return dispatched

    async def record_outcome(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        document_id: str,
        title: str,
        ok: bool,
        reason: str | None = None,
    ) -> IndexBuildVersion | None:
        """一篇构建子任务跑完后记账：推进进度；失败则追加明细（**不动文档状态**）。

        ## 为什么失败不改文档状态

        构建子任务失败时那篇文档本身没坏——线上旧版本照常可查、检索照常命中。把它标成
        ``failed`` 会让用户看到「失败」却发现内容好好的，两者矛盾。所以失败只落在**构建行**
        的明细里（ADR-0034 决策九），文档状态保持成功。跳过（已删 / 续建命中也算一堆）不记
        失败、只推进进度。

        ## 为什么用语句而不是改对象

        ``documents_done`` 是**并发写**的：多个构建子任务各跑在自己的会话里，读-改-写必然
        丢更新。那条 ``UPDATE ... SET documents_done = documents_done + 1`` 让数据库做加法，
        并发因此不需要任何锁。明细同理用 JSONB 的 ``||`` 追加。

        ## 返回值

        更新后的行（``None`` 表示这行已经不是 ``building`` 了：重复投递 / 迟到任务）。
        调用方拿到它才能再 :meth:`converge` 一次——「谁看见谁收尾」。
        """

        document_title = title or document_id
        if ok:
            await index_version_repository.advance_progress(db, build_id, tenant_id=tenant_id, by=1)
        else:
            await index_version_repository.record_failure(
                db,
                build_id,
                tenant_id=tenant_id,
                entry={
                    "document_id": document_id,
                    "title": document_title,
                    "reason": reason or BUILD_DOCUMENT_FAILED_REASON,
                },
            )
        await async_commit_now(db)
        return await self.converge(db, build_id, tenant_id=tenant_id, final=False)

    async def mark_redispatched(self, db: AsyncSession, build_id: str, *, tenant_id: str) -> int:
        """把这一行的重投次数 +1，返回新值；超上限的行收成 ``failed``（ADR-0034 决策七）。"""

        count = await index_version_repository.bump_redispatch(db, build_id, tenant_id=tenant_id)
        await async_commit_now(db)
        if count > MAX_REDISPATCH:
            await self.mark_failed(db, build_id, tenant_id=tenant_id, reason=BUILD_FAILED_MESSAGE)
        return count

    # ── 四项计数 ─────────────────────────────────────────────────

    async def collect_counts(
        self, db: AsyncSession, *, kb: KnowledgeBase, version: str
    ) -> BuildCounts:
        """把四项计数的读数取回来（各自从它真正的存储里读，见 :class:`BuildCounts`）。

        为什么分四次读而不是写一条大 SQL：它们**不在同一个存储里**。切片在
        PostgreSQL，向量在向量存储（本实现同表，换实现就不同），关键词文档在 ES。
        想「一条语句查完」就只能假装它们同源，而那道闸门要挡的正是它们不同源时的偏差。
        """

        tenant_id = str(kb.tenant_id)
        kb_id = str(kb.id)
        return BuildCounts(
            knowledge_bases=1,
            chunks=await chunk_repository.count_by_index_version(
                db, kb_id, tenant_id=tenant_id, index_version=version
            ),
            vectors=await self._vector_store.count_by_scope(
                db, tenant_id=tenant_id, kb_id=kb_id, index_version=version
            ),
            keyword_documents=(
                await self._keyword_search.count_documents(
                    tenant_id=tenant_id, kb_id=kb_id, index_version=version
                )
                if self._keyword_search.available
                else None
            ),
        )

    # ── ready / 激活 ──────────────────────────────────────────────

    async def mark_ready(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        counts: BuildCounts,
    ) -> IndexBuildVersion:
        """四项计数相等才允许 ``ready``；不相等就**拒绝**（抛异常，不改状态）。

        为什么是「拒绝」而不是「顺手标成 failed」：这个函数只回答「能不能标记为可用」，
        把失败留痕留给调用方（:meth:`build` 会带着具体原因去落 ``failed``）。混在一起
        之后，用例就没法单独断言「计数不相等时 ready 被拒」这条验收。
        """

        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        ensure_transition(row, INDEX_BUILD_READY)
        if not counts.matched:
            logger.warning(
                "index build counts do not match",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=row.kb_id,
                version=row.version,
                **counts.as_dict(),
            )
            raise IndexBuildCountsMismatch(counts)

        row.chunk_count = counts.chunks
        row.status = INDEX_BUILD_READY
        await async_commit_now(db)
        logger.info(
            "index build marked ready",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            **counts.as_dict(),
        )
        return row

    async def activate(
        self, db: AsyncSession, build_id: str, *, tenant_id: str
    ) -> IndexBuildVersion:
        """**单事务**激活：旧版本 ``archived`` + 新版本 ``active`` + 库列更新。

        三者同成同败（ADR-0025 决策三）。分开做的中间态是「库指向新版本、新版本还没
        active」，检索会拿到一个不存在的生效版本 → 一条都匹配不上 → 一次「诚实的空结果」
        被包装成「本次没命中」，于是故障看起来像查询问题。

        关键词别名的切换**排在提交之前**，并且不参与数据库事务（ES 不在 PG 的事务里）：

        * 先切别名的窗口里，别名指向新物理索引而库列仍是旧版本——关键词那一路按
          ``(库, 旧版本)`` 过滤新索引里的行，**一条都不匹配**，于是这一路诚实返回空，
          而不是混排两个版本（那正是决策二要防的）；
        * 反过来（先提交、后切别名）留下的窗口更糟：库说新版本、关键词仍查旧索引，
          两路数据**对不上**，而且如果切换失败就一直错下去。

        于是顺序是：切别名 → 提交事务；提交失败就把别名切回去（尽力而为，失败只记
        warning——那时真正该看的是提交失败本身）。

        这条入口只服务 ``ready → active``。``archived → active`` 是
        :meth:`rollback`（它多一条物理数据守卫，不能让激活顺路把它绕过去）。
        """

        return await self._switch_active_version(
            db, build_id, tenant_id=tenant_id, origin_status=INDEX_BUILD_READY
        )

    async def rollback(
        self, db: AsyncSession, build_id: str, *, tenant_id: str
    ) -> IndexBuildVersion:
        """**回滚**：把 ``archived`` 的那一版切回 active（#155，spec 用户故事 33）。

        动作与 :meth:`activate` 逐条相同——两条入口都进
        :meth:`_switch_active_version`，差别只有「从哪个版本出发」。**为什么不能抄一份**：
        抄出来的第二份会漏掉「归档当前 active」「回切别名」中的某一步，而两种遗漏在检索
        侧都表现为「库说 A、数据在 B」，且不报错。

        回滚多一条激活不需要的守卫：目标版本的**物理数据必须还在**。``ready`` 那一侧
        刚被四项计数验过（#142），而归档版本可能在很久以前就被清理过——它是不是空的，
        只有在这里问一句才知道。
        """

        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        # 先裁决「这一行配不配当回滚目标」，再问「它的数据还在不在」：状态不对的行
        # （building / failed）要用「从哪到哪」那条话术拒绝，不该被数据守卫抢答成
        # 「数据没了」——那是两种运维处置。共用那一段会再验一次真值表（纯函数，重复无害）。
        ensure_activation_origin(row, origin_status=INDEX_BUILD_ARCHIVED)
        kb = await knowledge_base_repository.get_by_id_for_tenant(
            db, row.kb_id, tenant_id=tenant_id
        )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        await self._ensure_rollback_has_data(db, kb=kb, row=row)
        return await self._switch_active_version(
            db,
            build_id,
            tenant_id=tenant_id,
            origin_status=INDEX_BUILD_ARCHIVED,
            kb=kb,
            row=row,
        )

    async def _ensure_rollback_has_data(
        self, db: AsyncSession, *, kb: KnowledgeBase, row: IndexBuildVersion
    ) -> None:
        """回滚守卫：目标版本的**物理数据仍在**（物理索引在、切片数 > 0）。

        两个读数来自两个存储（ES 与 PostgreSQL），哪一个为「没有」都拒绝：向量那一路要
        按版本从 ``chunks`` 里召回，关键词那一路要按别名查物理索引。缺任何一半，切过去之后
        对应的那一路就静默返空。

        「能力不存在」与「数据不存在」在这里**必须分开**：没部署 ES 的部署里物理索引永远
        不存在（``available`` 为 ``False``），把这一维当成「数据没了」会让回滚在那种部署
        上永远不可用——与 #142 把 ``keyword_documents`` 记成 ``None`` 是同一口径。
        """

        keyword_index = (
            await self._keyword_search.physical_index_exists(row.version)
            if self._keyword_search.available
            else None
        )
        chunks = await chunk_repository.count_by_index_version(
            db, str(kb.id), tenant_id=str(kb.tenant_id), index_version=row.version
        )
        if keyword_index is False or chunks <= 0:
            logger.warning(
                "index version cannot be rolled back to",
                build_id=row.id,
                tenant_id=row.tenant_id,
                kb_id=row.kb_id,
                version=row.version,
                keyword_index_exists=keyword_index,
                chunks=chunks,
            )
            raise IndexBuildRollbackRefused(
                version=row.version, keyword_index=keyword_index, chunks=chunks
            )

    async def _switch_active_version(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        origin_status: str,
        kb: KnowledgeBase | None = None,
        row: IndexBuildVersion | None = None,
    ) -> IndexBuildVersion:
        """把这一行切成 active：别名 → 一笔事务（归档旧 active + 本行 active + 库列）。

        **激活与回滚的同一段实现**：唯一的差别是 ``origin_status``（``ready`` 还是
        ``archived``）。真值表裁决合法性、入口声明起点（见
        :func:`ensure_activation_origin`），两道门因此不能互相冒充：激活走不到回滚的物理
        数据守卫上，回滚也不会被当成「第二次激活」。

        ``row`` / ``kb`` 可以由调用方传进来（回滚为了跑守卫已经读过它们）：同一个动作不必
        为此多查两次库，而两次读之间还留着「读到的不再是同一行」的窗口。
        """

        if row is None:
            row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "index build not found")
        ensure_activation_origin(row, origin_status=origin_status)

        if kb is None:
            kb = await knowledge_base_repository.get_by_id_for_tenant(
                db, row.kb_id, tenant_id=tenant_id
            )
        if kb is None:
            raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

        previous = await index_version_repository.find_active(db, row.kb_id, tenant_id=tenant_id)
        previous_version = previous.version if previous is not None else None

        # 收尾只能读普通值（#153 在真库上抓到）：那一笔事务一滚，ORM 行就过期了，再读属性
        # 会触发一次同步 lazy load —— 异步会话上那是 ``MissingGreenlet``，于是「提交失败」
        # 被第二个异常顶掉，别名还留在错的版本上（库说 v2、关键词查 v1；不报错，只是关键词
        # 一路静默变空）。所以三样身份先抄出来。
        target_version = str(row.version)
        build_id_value = str(row.id)
        kb_id_value = str(row.kb_id)
        tenant_id_value = str(row.tenant_id)

        switched = False
        try:
            await self._keyword_search.activate_version(target_version)
            switched = True
            async with async_transaction(db):
                stamp = now()
                if previous is not None:
                    previous.status = INDEX_BUILD_ARCHIVED
                    previous.archived_at = stamp
                    # 部分唯一索引 ``uq_index_build_versions_active`` 是**立即**校验的：
                    # 两条 UPDATE 必须「先归档旧的、再激活新的」。ORM 的 flush 顺序不由
                    # 调用顺序决定——真库上见过它把激活排在前面，直接 UniqueViolation
                    # （#153 的回滚首跑）。所以这里显式落一次，顺序不再靠运气。
                    await db.flush()
                row.status = INDEX_BUILD_ACTIVE
                row.activated_at = stamp
                # 回滚上线的版本不再有「归档于」：留着它，查「最近归档了哪几版」会说谎。
                row.archived_at = None
                # 库上的生效版本与状态一起改：检索范围从这一列读（决策四）。
                kb.active_index_version = row.version
        except IntegrityError as exc:
            # 并发的另一次切换先提交了：两条部分唯一索引在这里挡住「同一库两个 active」。
            # 回滚与激活看到的是**同一条**可读拒绝，而不是一次 500。
            await self._discard_failed_switch(db, build_id=build_id_value)
            if switched:
                await self._restore_alias(
                    db,
                    build_id=build_id_value,
                    kb_id=kb_id_value,
                    tenant_id=tenant_id_value,
                    version=target_version,
                    fallback=previous_version,
                )
            raise IndexBuildSwitchConflict() from exc
        except BaseException:
            await self._discard_failed_switch(db, build_id=build_id_value)
            if switched:
                await self._restore_alias(
                    db,
                    build_id=build_id_value,
                    kb_id=kb_id_value,
                    tenant_id=tenant_id_value,
                    version=target_version,
                    fallback=previous_version,
                )
            raise

        logger.info(
            "index build activated",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            previous_version=previous_version,
            origin_status=origin_status,
        )
        return row

    async def _discard_failed_switch(self, db: AsyncSession, *, build_id: str) -> None:
        """把失败的那一笔切换**彻底扔掉**，再让调用方去读「此刻谁生效」。

        **顺序不能换**（#156）：不先回滚就读库列，读到的是这一笔自己写进去、却从未提交的
        值（``kb.active_index_version = row.version`` 在提交之前就写了）——别名于是被「切回」
        到那个从未生效的版本上，与要修的 bug 一模一样，只是换了个来源。这在真库上同样成立：
        事务还开着时读到的就是同一事务里的脏值，所以这不是替身特有的讲究。

        回滚本身失败只记 warning：这条路径已经在一个失败动作里，真正要看的是那次失败。
        """

        try:
            await async_rollback_now(db)
        except Exception:  # noqa: BLE001 - 收尾路径不抛：真正的原因是那次切换失败
            logger.warning(
                "failed index switch could not be rolled back",
                build_id=build_id,
            )

    async def _restore_alias(
        self,
        db: AsyncSession,
        *,
        build_id: str,
        kb_id: str,
        tenant_id: str,
        version: str,
        fallback: str | None,
    ) -> None:
        """提交失败后把关键词别名切回**此刻真正生效的那一版**（尽力而为；失败只记 warning）。

        **为什么不是「切换前读到的那一版」**（#156）：并发里有两条切换在同一个库上跑，
        输掉的那一方读到的 ``previous`` 只是它自己开始时的现场。赢家提交之后，库里生效的
        已经是另一版——照旧值回切，别名就落在一个**谁也不认识**的版本上：向量那一路按库列
        查新版、关键词那一路查旧版的物理索引，一条都不匹配。表现为「关键词一路忽然全空」，
        而库行、切片、别名写操作全都成功，排障会从「检索是不是坏了」查起。

        所以这里**重读库列**（``active_index_version`` 是检索范围唯一的来源，与
        :func:`app.models.index_version.resolve_index_versions` 同一个口径）：读到什么就
        切回什么。

        两条退路都写在这里，而不是让异常往上冒：

        * **读不到**（库没了、会话已不可用——例如本笔并入了外层事务而外层已中止）时退到
          ``fallback``（切换前读到的那一版），并记一条 warning。错误路径上再抛第二个异常，
          会把「提交为什么失败」这条真正的原因盖掉；
        * ``fallback`` 也是 ``None``（这个库的第一次构建）时保持原样：别名留在了未提交的
          新版本上，检索仍安全（库列还是空的，守卫直接拦），下一轮切换会把别名再挪一次。
        """

        live = await self._live_version_now(db, tenant_id=tenant_id, kb_id=kb_id)
        if live is not None and live != fallback:
            logger.warning(
                "keyword alias restore follows the version that is live now",
                build_id=build_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                live_version=live,
                previous_version=fallback,
            )
        target = live if live is not None else fallback
        if target is None:
            logger.warning(
                "keyword alias left on an uncommitted version",
                build_id=build_id,
                version=version,
            )
            return
        try:
            await self._keyword_search.activate_version(target)
        except Exception:  # noqa: BLE001 - 收尾路径不抛：真正的原因是那次提交失败
            logger.warning(
                "keyword alias restore failed",
                build_id=build_id,
                previous_version=target,
            )

    async def _live_version_now(
        self, db: AsyncSession, *, tenant_id: str, kb_id: str
    ) -> str | None:
        """重读「这个库此刻生效哪一版」；读不到就返回 ``None``（由调用方决定退路）。

        **读的是库列、不是版本行**：检索范围只认 ``active_index_version``（决策四），
        版本行的 ``active`` 状态是同一笔事务里写的另一半——两者不在一处时，跟着检索实际
        用的那一列走才对得上。

        失败一律收成 ``None``：这条路径跑在**已经失败**的一次切换上，再抛一个异常只会把
        根因藏起来（与 :meth:`_restore_alias` 同一条纪律）。
        """

        try:
            kb = await knowledge_base_repository.get_by_id_for_tenant(
                db, kb_id, tenant_id=tenant_id
            )
        except Exception:  # noqa: BLE001 - 错误路径上的读失败不追加异常
            logger.warning(
                "keyword alias restore could not re-read the live version",
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            return None
        if kb is None:
            return None
        return effective_index_version(kb)

    # ── 失败 ────────────────────────────────────────────────────

    async def settle_failure(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        cause: BaseException,
    ) -> IndexBuildVersion | None:
        """按异常挑一个原因，把这次构建收成 ``failed``。

        **映射只有这一处**：任务体（:meth:`build`）与运维脚本（``scripts/reconcile_build.py``）
        都会在一次收尾失败之后落到这里，两处各写一份 ``isinstance`` 判断的下场是——某一天
        其中一处把「四项计数不一致」降级成了笼统的「跑崩了」，而运维照原因去查时查错了方向
        （一因一码：这两种失败的处置完全不同）。
        """

        reason = (
            BUILD_COUNTS_MISMATCH_MESSAGE
            if isinstance(cause, IndexBuildCountsMismatch)
            else BUILD_FAILED_MESSAGE
        )
        return await self.mark_failed(db, build_id, tenant_id=tenant_id, reason=reason)

    async def mark_failed(
        self,
        db: AsyncSession,
        build_id: str,
        *,
        tenant_id: str,
        reason: str = BUILD_FAILED_MESSAGE,
    ) -> IndexBuildVersion | None:
        """把这个版本收成 ``failed`` + 固定原因；**不碰** ``active_index_version``。

        返回 ``None`` 表示这一行不存在（跨租户 / 被删），调用方不必区分——失败路径再多
        一层「没有这一行」的异常，只会把真正的原因盖掉。
        """

        row = await index_version_repository.get_for_tenant(db, build_id, tenant_id=tenant_id)
        if row is None:
            return None
        if not _is_failure_allowed(row):
            logger.warning(
                "index build failure arrived late, status kept",
                build_id=build_id,
                tenant_id=tenant_id,
                status=row.status,
            )
            return row
        row.status = INDEX_BUILD_FAILED
        row.error_message = reason
        await async_commit_now(db)
        logger.info(
            "index build failed",
            build_id=build_id,
            tenant_id=tenant_id,
            kb_id=row.kb_id,
            version=row.version,
            reason=reason,
        )
        return row

    # ── 「有行、没任务」 ─────────────────────────────────────────

    async def list_stalled_builds(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        older_than_seconds: int = BUILD_STALL_SECONDS,
        at: datetime | None = None,
    ) -> list[IndexBuildVersion]:
        """指出那些「还是 building、但很可能已经没有任务在跑」的行。

        进程被 SIGKILL、机器重启、Worker 队列被清空——这些情况下库里只剩一行 ``building``，
        没有任何东西会去收尾。它**不影响线上**（active 仍是旧版本），但会挡住下一次构建
        （一个库只允许一个 building），所以必须能被看见。判定只用年龄：库里读不到「任务
        还在不在」。
        """

        started_before = (at or now()) - timedelta(seconds=older_than_seconds)
        rows = await index_version_repository.list_stalled(
            db, tenant_id=tenant_id, started_before=started_before
        )
        if rows:
            logger.warning(
                "index build has no task",
                tenant_id=tenant_id,
                build_ids=[row.id for row in rows],
                kb_ids=[row.kb_id for row in rows],
                versions=[row.version for row in rows],
                older_than_seconds=older_than_seconds,
            )
        return rows

    async def list_undone_builds(
        self,
        db: AsyncSession,
        *,
        tenant_id: str | None = None,
        kb_id: str | None = None,
    ) -> list[IndexBuildVersion]:
        """**还没建完**的构建（清扫器「缺篇补投」用的名单，#225）。

        与 :meth:`list_overdue_builds` 的判据正交：那条问「死线过了没」，这条问「篇够了没」。
        清扫器两样都要，因为它们的处置不同——过期就收尾（别再等了），没过期但缺篇就补投
        （还有救）。

        ``tenant_id`` 可省略是给跨租户的清扫器留的（只读、每行自带租户，理由同
        :meth:`list_overdue_builds`）。
        """

        return await index_version_repository.list_undone(db, tenant_id=tenant_id, kb_id=kb_id)

    async def list_overdue_builds(
        self,
        db: AsyncSession,
        *,
        at: datetime | None = None,
        tenant_id: str | None = None,
        kb_id: str | None = None,
    ) -> list[IndexBuildVersion]:
        """**过了收敛期限仍是 ``building``** 的构建 = 「该收敛却没收敛」（#214）。

        这是给自愈票（清扫器）留的判据，本票**不实现清扫器**：谁看见这个名单，谁就按真值
        收一次尾（:meth:`converge`，幂等）。

        与 :meth:`list_stalled_builds` 的差别是**判据换了**：那条按「年龄」判（一小时前开始
        的就可疑），这条按**构建行自己的租约**判（期限按参与篇数估出来，一篇一小时的库与
        一篇八秒的库不该用同一个阈值）。两条都留着，是因为它们回答两个不同的问题：
        「这个进程是不是根本没跑」与「这次构建到底该不该等」。期限这一条才是权威判据。

        ``tenant_id`` 可省略，只服务跨租户的清扫器；它**只读**，每一行都自带租户（写动作仍
        由调用方带明确租户去做，见 ``index_version_repository.list_overdue``）。
        """

        moment = at or now()
        rows = await index_version_repository.list_overdue(
            db, at=moment, tenant_id=tenant_id, kb_id=kb_id
        )
        if rows:
            logger.warning(
                "index build passed its convergence deadline",
                tenant_id=tenant_id,
                kb_id=kb_id,
                build_ids=[row.id for row in rows],
                versions=[row.version for row in rows],
                deadlines=[row.converge_deadline.isoformat() for row in rows],
            )
        return rows


def describe_stalled_builds(rows: Sequence[IndexBuildVersion]) -> str:
    """把 :meth:`IndexBuildService.list_stalled_builds` 的结果拼成一行可读的话。

    给运维脚本 / 重启自检用：它要打印的是「哪个库、哪一版、什么时候开始的」，而不是一个
    列表对象的 ``repr``。
    """

    if not rows:
        return "no stalled index build"
    parts = [
        f"{row.kb_id}/{row.version} (build {row.id}, started {row.created_at.isoformat()})"
        for row in rows
    ]
    return "stalled index builds: " + "; ".join(parts)


def describe_overdue_builds(rows: Sequence[IndexBuildVersion]) -> str:
    """把 :meth:`IndexBuildService.list_overdue_builds` 的结果拼成一行可读的话。

    打印的是**期限**而不是开始时间：这条判据说的是「它欠一个结论已经欠了多久」，
    运维据此决定先看哪一个。
    """

    if not rows:
        return "no index build past its convergence deadline"
    parts = [
        f"{row.kb_id}/{row.version} (build {row.id}, deadline {row.converge_deadline.isoformat()})"
        for row in rows
    ]
    return "index builds past their convergence deadline: " + "; ".join(parts)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "BUILD_ALREADY_RUNNING_MESSAGE",
    "BUILD_COUNTS_MISMATCH_MESSAGE",
    "BUILD_FAILED_MESSAGE",
    "BUILD_INCOMPLETE_MESSAGE",
    "BUILD_INVALID_TRANSITION_MESSAGE",
    "BUILD_NOTHING_TO_REBUILD_MESSAGE",
    "BUILD_STALL_SECONDS",
    "CONVERGE_BASE_SECONDS",
    "CONVERGE_MAX_SECONDS",
    "CONVERGE_PER_DOCUMENT_SECONDS",
    "CONVERGE_WAIT",
    "BuildCounts",
    "BuildTruth",
    "IndexBuildAlreadyRunning",
    "IndexBuildCountsMismatch",
    "IndexBuildIncomplete",
    "IndexBuildInvalidTransition",
    "IndexBuildNothingToRebuild",
    "IndexBuildRollbackRefused",
    "IndexBuildService",
    "IndexBuildSwitchConflict",
    "ROLLBACK_DATA_MISSING_MESSAGE",
    "SWITCH_CONFLICT_MESSAGE",
    "can_transition",
    "converge_deadline_for",
    "decide_convergence",
    "describe_overdue_builds",
    "describe_stalled_builds",
    "ensure_activation_origin",
    "ensure_transition",
]
