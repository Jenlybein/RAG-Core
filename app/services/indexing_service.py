"""索引流水线（Worker 侧）：解析 → 切块 → 拼检索文本 → 向量化 → 先删后写 → 状态回写。

上传请求线程只登记并投递任务，绝不进来这里；进程内的耗时（尤其是 embedding 的
网络往返）完全由 Celery Worker 承担。

**两段事务**是这里最重要的不变量：
1. ``try_claim_for_indexing`` 抢到索引权后立刻 ``commit``。若把整条流水线包在
   行锁里，``GET /documents`` 在 Worker 跑完之前都会卡在这份文档那一行上。
   **抢到的那一次调用还把租约令牌带回来**（#213）：这一条语句实际写进库的
   ``index_claimed_at``，Tx2 的开头用它核对「这一行还是不是我的」。
2. 解析 / 切块 / 向量化之后，purge（先删旧切片）+ 写入新切片 + ``status=SUCCESS``
   在同一段事务里提交：调用方永远不会看到「旧切片没了、新切片还没写」或
   「切片写好了、状态还是处理中」的中间态。异常时由调用方（Celery 任务）回滚。

**Tx2 开头核对租约令牌（#213）**：Tx1 提交之后、Tx2 提交之前，租约可能过期并被另一个任务
接走。若不管，旧任务醒来会 purge 掉新主人刚写的切片、再把自己的内容写回去，最后把状态从
「索引中」改回「成功」——现象是「重建完了，检索到的却是旧内容」。令牌不等就整笔放弃
（回滚 + warning + 返回 0），调用方按「跳过」处理，不重试。

**第 11 步票 #132 起，这条流水线跑在异步栈上**：会话是 ``AsyncSession``（Worker 常驻
循环里那一个，见 ``app/db/worker_loop.py``），两次提交走 ``async_commit_now``——提交点
仍然只来自 ``app.db.transaction``。**解析仍然同步**（``prepare_document_content`` 与解析
库不改）：Celery prefork 每个子进程同一时刻只跑一个任务，把 CPU 活丢进线程只会多一层，
换不来任何并发（ADR-0023「并发由进程数给，不由循环数给」）。

**索引权只由 Worker 抢占。** 入队侧（上传 / 重建 / 建版本 / 回滚 / 运维脚本）已经用
``prepare_for_indexing`` 把文档置成「排队中」（``status=4``，不盖租约），Worker 进来后
用 ``try_claim_for_indexing(allow_success=False)`` 盖租约。``allow_success=False``
是必须的：force 重建的路由已经做过「成功 → 排队中」的状态准备，ACK 丢失导致的
迟到重复投递不能在 SUCCESS 之后重新抢占并重写切片。

并发去重靠数据库 CAS，不靠 Celery 的 ``task_id``：``acks_late`` 让投递至少一次，
而 ``try_claim_for_indexing`` 让重复投递里的后来者抢不到，直接返回 0。这两者是
配套的，不要只留一半。

所有依赖（切块器 / embedding / 存储列表）都从构造参数注入，任务里绝不在方法体
内 ``new`` 具体类：测试靠注入替身，整份测试不需要网络、Broker 或数据库。

**解析成功后正文写回文档行（#170）。** 在那之前，文件上传的文档 ``documents.content``
永远是空串：每次重建都回磁盘重新解析，术语表里「文档正文 = 整份文档经解析后的全文」
只是一句空话。回写落在 Tx2（与 purge / 写入切片 / 状态回写同一笔），所以「正文、
切片、状态」永远一起变；此后不带「重解析」的重建读库内正文，要重读磁盘必须显式要求。

**送进 embedding 的是检索文本，落库的 content 仍是切片原文。** 检索文本由
``build_retrieval_text`` 用「文档名 + 章节路径 + 正文」拼成，不落库：向量与
关键词索引共用这一份文本，两路对比才有意义；调用方拿到的 ``content``
必须还是原文（见 GLOSSARY 的「检索文本」与「切片原文」）。所以 chunk 字典里
同时带 ``content``（原文，落库）与 ``retrieval_text``（检索文本，进向量与关键词
索引），后者由向量存储忽略、由关键词 Provider 使用。

**双写遍历的是同一个列表。** 向量存储与关键词检索都满足
``app.services.chunk_store.ChunkStore`` 的写入契约，默认接线由
``build_chunk_stores`` 组装；加第三个存储只需要往列表里再加一个元素。索引阶段
不做降级：任一处写失败就是索引失败（由 Celery 任务回滚并置为失败），否则会留下
「向量库有、关键词库没有」的数据不一致。

**这一片属于第几代（现场九·第二步）**：切片字典顶层的 ``generation`` 落 ``chunks.generation``
真实列，可见性由它和 ``documents.current_index_generation`` 的列比较决定。库级索引版本
那一层（``chunks.index_version`` / 库的生效版本 / 版本表 / 版本化物理索引）已经整层删除：
写入不再有「目标版本」这个参数，产物属于哪一代由这一篇自己的代表达。

**这一片属于第几稿（#173）**：切片字典顶层的 ``doc_version`` 落 ``chunks.doc_version``
真实列，清除也按它收窄——重建第 2 稿时第 1 稿的切片原样留着（ADR-0005 D4 选 B）。
稿号有两个可能的来源：调用方显式给的（任务载荷，见 ``app.services.index_dispatch``）
优先，没给才回落文档行的当前稿指针。**迟到的任务因此不会写错稿**：它的稿号在投递那
一刻就固定了，不再向指针二次确认。

**重建的正文从版本表读（#173 / ADR-0005 D6）**：``documents.content`` 只是热路径快照，
真值是 ``document_versions.content``。指针刚切过时两者会不一致，而错的那一份恰好是
文档行——拿它切块会写出「标着第 N 稿、装着别的稿正文」的切片。重解析（``reparse=True``）
是唯一例外：它的全部意义就是再读一遍源文件。没有版本行时回落文档行（建版本的写路径
在 #175 才落地，历史数据只有文档行那份），但若调用方显式指定的是**过期稿次**则拒绝——
那时回落等于静默写错。

**重解析的产物写回同一稿的版本行（#182）。** 重解析不是改稿：同一份原始文件换一个
解析器再读一遍，稿次没变。所以它的正文 **写回那一稿已有的版本行**（正文 + 校验和一起
换，稿号不变、不新增行、不动指针与归档状态），而不是只覆盖文档行——只覆盖文档行会让
「正文真值在版本表」当场分叉，而 T10 对账的第一条硬检查正是「文档正文 == 当前稿正文」。
顺带纠正了另一半：读的是**这一稿自己的**源文件（``<doc>.v<N><ext>``），不是文档行上
那份（它只指向最新一次上传）。文档行正文只在这一稿就是当前稿时跟着改——迟到的重解析
任务算的是旧稿，它解析出来的正文不是文档行该显示的那一稿。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import async_commit_now, async_rollback_now
from app.models.document import Document
from app.models.document_version import DOCUMENT_VERSION_CURRENT, DocumentVersion
from app.models.knowledge_base import SPLIT_SIZE, SPLIT_STRUCTURE, SplitStrategy
from app.providers.embedding import EmbeddingProvider, get_embedding_provider
from app.services.index_generation import next_generation
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.parsers.base import ParsedDocument
from app.providers.vectorstores import get_vector_store
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PlanSpec
from app.repositories import (
    document_repository,
    document_version_repository,
    knowledge_base_repository,
    tenant_repository,
)
from app.services import chunk_ledger, plan_service, static_quota
from app.services.chunk_store import ChunkStore, build_chunk_stores
from app.services.document_ingestion import (
    content_from_version,
    parse_source_file,
    prepare_document_content,
)
from app.services.section_context import summarize_sections
from app.utils.checksum import checksum_of
from app.utils.tokenizer import token_count
from app.utils.id_generator import new_chunk_id, new_version_id
from app.utils.retrieval_text import build_retrieval_text
from app.utils.status import DocumentStatus
from app.utils.splitter_factory import build_splitter
from app.utils.text_splitter import WindowTextSplitter
from app.utils.text_splitter import TextSplitter

logger = structlog.get_logger(__name__)

# 索引链路失败时回传给调用方的唯一原因文案：不含异常对象、堆栈、路径或 SQL。
# 失败回写由 app.tasks.indexing._mark_failed 负责，它从这里 import；不要把异常
# 原文拼进来。
INDEXING_FAILED_MESSAGE = "document indexing failed, please retry or contact support"


async def purge_document_chunks(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    stores: list[ChunkStore],
    doc_version: int | None = None,
    generation: int | None = None,
) -> None:
    """先删后写的第一半：遍历**存储列表**，逐个清除该文档的旧切片。

    遍历的是列表而不是写死某一个存储：向量存储与关键词检索实现同一个写入契约
    （见 ``app.services.chunk_store``），所以以后加第三个存储只需在默认接线里
    往 ``stores`` 追加一个元素，调用方一行都不用改。``tenant_id`` 是强制过滤参数
    （ADR-0006），连删除也必须带租户。

    ``generation`` 是**代的范围**（现场九·第五步）：重建写新代时只清新代（通常是
    空集），上一代原样留着——「保留最近 2 代」与「单篇切回上一代」都靠它成立。

    ``doc_version`` 是**稿号范围**（#173）：重建第 2 稿时只清第 2 稿的切片，
    第 1 稿原样留着（ADR-0005 D4 选 B「每稿各留切片」）；``None`` 表示「这篇文档的
    所有稿」——删文档那条老路径仍用它，行为与引入稿号前逐字相同。

    没有库级版本范围（``chunks.index_version`` 已随版本层删除）：清理只按「代 + 稿」
    收窄，可见性也只由这两轴表达。

    会话类型是 ``AsyncSession``：Worker 索引、数据面删除与控制面删除都在异步栈上
    （第 11 步票 #132 / #130）。
    """

    for store in stores:
        await store.delete_by_document_id(
            db,
            document_id,
            tenant_id=tenant_id,
            doc_version=doc_version,
            generation=generation,
        )


class _ContentSource(NamedTuple):
    """这次索引的正文从哪来，以及它该写回**哪一行**版本记录（#182）。

    ``reparsed`` 为真表示这份正文是「这次重新解析源文件」的产物——同一份原始文件换了个
    解析器再读一遍，稿次没变，所以它要落回 ``version_row`` 那一行（正文 + 校验和一起换），
    而不是新增一稿。为假时正文本来就来自版本表或文档行，没有要写回的东西。

    ``version_row`` 为 ``None`` 表示这一稿还没有版本行（#172 之前的存量数据、刚上传还没
    索引过的文档）：缺的那一条由 :meth:`IndexingService._ensure_version_row` 补。
    """

    parsed: ParsedDocument
    version_row: DocumentVersion | None = None
    reparsed: bool = False


def _reparse_source(doc: Document, version_row: DocumentVersion | None) -> ParsedDocument:
    """重解析读**哪一份字节**：这一稿自己的源文件优先（#182）。

    ## 为什么不是文档行上的路径

    每一稿的源文件都按稿号落盘（``<doc>.v<N><ext>``，见
    :func:`app.services.document_storage.store_version_file`），而 ``documents`` 上的
    那一列只指向**最新一次上传**。重解析第 1 稿时读文档行，解析到的是第 2 稿的字节——
    症状是「重建之后内容莫名其妙变了」，既不报错也不留痕，而它正是本票要堵的洞。

    ## 没有源文件时不回落

    目标稿没有可读源文件（JSON 提交的稿次、文件已从盘上消失）就报 ``DOCUMENT_PARSE_FAILED``。
    回落读文档行那份只有在**这一稿还没有版本行**时才成立（#172 之前的存量文档全靠它），
    那时没有别的字节可读，也就没有「读错稿」的风险。
    """

    if version_row is None:
        return prepare_document_content(doc, reparse=True)
    return parse_source_file(version_row.source_file_path, filename=doc.source_filename)


class IndexingService:
    """Worker 侧的索引编排：一份已登记的文档 → 可检索的切片。"""

    def __init__(
        self,
        *,
        splitter: TextSplitter | None = None,
        embeddings: EmbeddingProvider | None = None,
        stores: list[ChunkStore] | None = None,
        heartbeat_session_factory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        # 注入的切块器优先：测试用它把切块与接线解耦。生产路径不注入，
        # 每份文档在索引时按**所属知识库**的配置现建切块器。
        self._splitter = splitter
        self._embeddings = embeddings if embeddings is not None else get_embedding_provider()
        # 默认接线就是双写：向量存储 + 关键词检索。没配关键词引擎时拿到的是空实现，
        # 它照常占一个位置（能力明确不可用，而不是「索引链路少接了一段」）。
        self._stores = (
            stores
            if stores is not None
            else build_chunk_stores(get_vector_store(), get_keyword_search_provider())
        )
        # 心跳用**独立会话**（#225）：见 ``_heartbeat`` 的注释——用流水线那个会话提交，会把
        # 内存里已经改过、但本应等到 Tx2 才落库的正文一起刷出去。
        self._heartbeat_sessions = heartbeat_session_factory

    async def _heartbeat(self, doc: Document, token: datetime) -> None:
        """刷新这一篇的心跳并立刻提交（#225）。

        ## 为什么立刻提交，而不是并进 Tx2

        心跳要证明的是「**此刻**还活着」。并进 Tx2 的话它要等到最后才落库——而 Tx2 正是
        「跑完了」那一刻，那时已经不需要证明了。

        ## 为什么必须用**独立会话**

        用它手里那个流水线会话提交，会把内存里已经改过、但**本应等到 Tx2 才落库**的东西一起
        刷出去——`doc.content = parsed.content` 就在心跳之前执行。于是「正文、切片、版本行、
        状态同一笔事务」这条纪律（第 11 步起就有的硬约束）被一次心跳悄悄破坏，库里会出现
        「正文是新的、切片还是旧的」的半份状态。独立会话只写 ``index_heartbeat_at`` 一列。

        ## 为什么丢了心跳不拦任务

        续租失败有两种可能：租约已被别人接走（那 Tx2 的 fencing 会把整笔放弃，回滚是安全的），
        或者只是这一条 UPDATE 出了问题。前者由 fencing 兜住，后者不该让一次已经跑了一半的
        索引作废——所以这里只记一条 warning，让流水线继续（真要出事，Tx2 会拒掉整笔）。
        """

        sessions = self._heartbeat_sessions
        heartbeat_db: AsyncSession | None = None
        try:
            if sessions is not None:
                heartbeat_db = sessions()
            else:
                from app.db.session_factory import async_session_factory

                heartbeat_db = async_session_factory()()
            renewed = await document_repository.renew_claim(
                heartbeat_db, doc.id, tenant_id=doc.tenant_id, token=token
            )
            await async_commit_now(heartbeat_db)
            await heartbeat_db.close()
        except Exception:  # noqa: BLE001 - 心跳失败不该让流水线中断（fencing 另有兜底）
            if heartbeat_db is not None:
                await async_rollback_now(heartbeat_db)
            logger.warning(
                "indexing heartbeat failed",
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
            )
            return
        if not renewed:
            logger.warning(
                "indexing heartbeat rejected: the lease no longer belongs to this task",
                document_id=doc.id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
            )

    async def _plan_of(self, db: AsyncSession, doc: Document) -> PlanSpec:
        """这篇文档所属租户的套餐快照（容量判据的输入，#220）。

        为什么由索引服务自己取、而不是让任务体传进来：任务体不该知道「容量上限」这件事
        （它只管调度），而索引服务是**做这件事的人**。取法与数据面完全同一份
        （``plan_service.resolve_plan`` + 租户行上的 ``plan_id``），所以「面板上改了套餐、
        下一次处理就按新值判」这一点在这里也成立。
        """

        tenant = await tenant_repository.get_by_id_async(db, doc.tenant_id)
        if tenant is None:
            # 租户行不见了却还有文档在跑：这是人手改库才会出现的状态。按最保守的一档判
            # （free），宁可拒一次也不要让一个已经没有归属的租户按高档位放行。
            return PLAN_PRESETS[PLAN_FREE]
        return await plan_service.resolve_plan(db, tenant.plan_id)

    async def _resolve_splitter(self, db: AsyncSession, doc: Document) -> tuple[TextSplitter, str]:
        """按文档所属知识库的配置构造切块器，返回 (切块器, 策略名)。

        策略与窗口必须来自知识库而不是全局配置：同一个库里混用两种切法会让片长
        分布不一致，检索时「片长」会变成与内容无关的排序因素。

        **文档 id 一定要带下去**（#143）：节的身份证是 ``stable_hash(文档 id : 标题路径)``，
        少了它，两份文档的「退款 > 流程」会算出同一个 ``section_id``，跨文档一对就串。
        切块器自己看不见文档行，所以这个参数只能由这里给。
        """

        if self._splitter is not None:
            # 注入的切块器如实报告它是哪一类：把按字符切的说成 structure，
            # 会让切片 meta 里的 split_strategy 变成一句谎话，重建时对不上。
            injected: SplitStrategy = (
                SPLIT_SIZE if isinstance(self._splitter, WindowTextSplitter) else SPLIT_STRUCTURE
            )
            return self._splitter, injected
        (
            strategy,
            chunk_size,
            section_part_max_chars,
        ) = await knowledge_base_repository.get_split_config(db, doc.kb_id, tenant_id=doc.tenant_id)
        splitter = build_splitter(
            strategy,
            chunk_size=chunk_size,
            document_id=str(doc.id),
            section_part_max_chars=section_part_max_chars,
        )
        return splitter, strategy

    async def _content_of(
        self,
        db: AsyncSession,
        doc: Document,
        *,
        draft: int,
        requested_draft: int | None,
        reparse: bool,
    ) -> _ContentSource:
        """这次索引的正文从哪来：**版本表优先**，读不到才谈回落（#173 / ADR-0005 D6）。

        三个分支，顺序是契约：

        1. ``reparse=True`` → 读源文件重解析。重解析的全部意义就是「再解析一遍」，
           版本行里那份正文正是上一次解析的产物，读它等于没重解析；**产物写回这一稿的
           版本行**（#182），于是「同一份原始文件换个解析器」不会把正文真值撕成两份；
        2. 版本表里有这一稿 → 用它。**正文的真值在版本表**，``documents.content``
           只是热路径快照（D6）。指针刚切过时这两份会不一致，而错的那一份恰好是
           文档行：拿它切块会写出「标着第 N 稿、装着别的稿正文」的切片；
        3. 版本表里没有这一稿：
           * 调用方**显式指定**了一个不是当前指针的稿号 → 拒绝。这是迟到任务的现场：
             它算的是旧稿，指针已经前移，文档行里装的是别的稿。这时回落去读文档行
             会静默写错；错误码是 20030「稿不存在」（``DOCUMENT_VERSION_NOT_FOUND``），
             不是 20016「文档不存在」——文档在，没有的是那一稿（ADR-0007：一个含义一个码）；
           * 否则回落文档行 / 磁盘（``prepare_document_content``）。建版本的写路径
             落地（#175）之前，库里本来就没有版本行；老数据与兜底脚本都靠这条路，
             缺了它一篇历史文档会永远索引不了。

        版本行在**分支 1 也要读**（尽管不用它的正文）：它决定重解析的产物写回哪一行，
        也决定读哪一份源文件。少了这次读，重解析就只能盲写文档行——那正是 #182 的洞。
        """

        version_row = await document_version_repository.get_by_version(db, str(doc.id), draft)

        # 分支 1：显式要求重解析。
        if reparse:
            return _ContentSource(
                parsed=_reparse_source(doc, version_row),
                version_row=version_row,
                reparsed=True,
            )

        if version_row is not None:
            # 分支 2：正文真值在版本表。
            return _ContentSource(
                parsed=content_from_version(version_row, source_type=doc.source_type),
                version_row=version_row,
            )

        # 分支 3：没有版本行。
        if requested_draft is not None and requested_draft != doc.current_version:
            # 「这一稿不存在」，不是「这篇文档不存在」：回 20016 会让调用方去查一份它手里
            # 有 id 的文档为什么「不存在」，而真因是那一稿被保留策略清掉了（#178）。
            raise AppError(
                ErrorCode.DOCUMENT_VERSION_NOT_FOUND,
                "要重建的稿次不存在",
                data={"version": requested_draft},
            )
        return _ContentSource(parsed=prepare_document_content(doc, reparse=False))

    async def index_existing_document(
        self,
        db: AsyncSession,
        document_id: str,
        *,
        reparse: bool = False,
        doc_version: int | None = None,
        allow_success: bool = False,
    ) -> int:
        """把一份已经登记在库的文档索引成切片，返回写入的切片数；跳过时返回 0。

        「跳过」有两种，调用方都按「这一篇没写」处理、都不要重试：抢不到租约（另一个
        Worker 正在跑），以及 Tx2 开头令牌核对失败（租约已被接走，见模块文档 #213）。

        ``reparse=True`` 强制重解析原始文件（忽略库内正文）；``allow_success=True`` 让
        **已经是 SUCCESS 的文档**也能被抢占——那是「按篇重建」要的：重建的输入本来就是
        成功文档。上传 / 重建路由仍走默认值（路由先用 ``prepare_for_indexing`` 做过
        「成功 → 排队中」的准备），迟到重复投递不能重新抢占成功文档。

        ``doc_version`` 是**这一片属于第几稿**（#173）：调用方显式给（任务载荷，见
        ``app.services.index_dispatch``）时一律以它为准；不传时回落文档行的当前稿指针
        ``documents.current_version``。**为什么必须能显式给**：指针是活数据，任务可能
        迟到——回滚刚把指针切回第 1 稿时，一个还在路上、本来算的是第 2 稿的任务若去读
        指针，就会把第 2 稿的内容写成第 1 稿的切片。稿号在投递那一刻就固定下来，
        Worker 不再向指针二次确认。

        写入哪一代由这一篇自己的指针算（``next_generation``），不由调用方指定：整库构建
        已经不存在，就没有「写进哪一版」这件事——每次索引都是这一篇的下一代。

        会话是 ``AsyncSession``（Worker 常驻循环里那一个），两段事务的提交点是
        ``app.db.transaction`` 的异步孪生 ``async_commit_now``；**提交次数与改造前
        逐条相同**：抢占后一次、purge + 写入 + 状态回写一次。异常路径不在这里回滚——
        由任务体（``app/tasks/indexing.py``）回滚并置失败，与同步栈时同一分工。
        """

        doc = await document_repository.get_by_id(db, document_id)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "document not found")

        # 这次索引写的是哪一稿：调用方给的优先（迟到任务不许改口），否则读指针。
        draft = doc_version if doc_version is not None else doc.current_version

        # Tx1：抢占后立刻提交，只持锁一瞬间。抢不到说明另一个 Worker 已经在跑
        # 这份文档（重复投递 / 重试），直接跳过，绝不重复向量化。
        #
        # 抢占回的是**租约令牌**（#213）：这一条语句实际写进库的 ``index_claimed_at``。
        # 下面 Tx2 的开头拿它核对「这一行还是不是我抢到的那一次」。
        claim_token = await document_repository.try_claim_for_indexing_async(
            db, document_id, tenant_id=doc.tenant_id, allow_success=allow_success
        )
        await async_commit_now(db)
        if claim_token is None:
            logger.info(
                "indexing skipped, another worker owns it",
                document_id=document_id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
            )
            return 0

        source = await self._content_of(
            db, doc, draft=draft, requested_draft=doc_version, reparse=reparse
        )
        parsed = source.parsed

        # 正文回写（#170）：解析成功后把全文写进文档行，此后**不带「重解析」的重建
        # 直接读库内正文**（``prepare_document_content`` 的分支 A），不再回磁盘。
        # 回写不单独提交：它与 purge、写入新切片、状态回写同属下面那笔 Tx2，任何一步
        # 失败都由任务体整笔撤销，不会留下「正文是新的、切片还是旧的」这种半份状态。
        #
        # **只在这一稿就是当前稿时回写**（#182）：迟到的重解析任务算的是旧稿，它解析
        # 出来的正文不是文档行该显示的那一稿，照写就会让「文档正文 == 当前稿正文」
        # 这条对账口径当场不成立。旧稿的产物落到**它自己那一行**版本记录上（见下面的
        # ``_record_version_content``），一个字都不丢。
        if draft == doc.current_version:
            doc.content = parsed.content

        # ── 单库 token 容量硬拦（#220 / ADR-0035 决策四）────────────────────
        #
        # 位置选在**切块之前**：容量不够时连切都不必切（切块与嵌入是真正花钱的两步）。
        # ── 心跳（#225）：解析刚结束，先证明「我还活着」──────────────
        #
        # 为什么在这里：解析是整条流水线最长的静默段（大 PDF 几十秒），而这段时间里租约
        # 一个字节都不会动——清扫器据此会把它当成「接了之后没声了」，然后抢走重投，于是
        # 同一篇被索引两遍（白烧一轮 embedding）。心跳只刷新 ``index_heartbeat_at``，
        # **不动** ``index_claimed_at``：那是 fencing 令牌，改了 Tx2 会把自己拒掉。
        #
        # 单独一次短提交，不等 Tx2：心跳的意义就是「还没到最后也要有活的证据」。
        await self._heartbeat(doc, claim_token)

        # 口径是**正文** token，与单库容量判据同一把尺子；每日额度那边记的是检索文本
        # token（多出标题与节说明前缀），两个数字各管一件事。
        tokens = token_count(parsed.content)
        # 套餐取一次、两处复用（容量判据与日额度）：查两遍**不会**得到两个答案，但会让
        # 「这两条上限来自同一份套餐」这件事在代码里看不出来。
        plan = await self._plan_of(db, doc)
        # 容量硬拦抛出的 ``AppError`` 会被 ``_mark_failed`` 写成 ``status=2`` + 一段**固定
        # 脱敏文案**。那段文案若是通用的英文句子，用户只看到「document indexing failed」——
        # 而真实原因是「这一篇 12 万 token，库上限 3 万」，这是两句完全不同的话。
        # 所以 ``_mark_failed`` 会按错误码选文案：容量型用**我们自己写的**带数字中文句
        # （不含异常原文，脱敏纪律不变），其余仍走通用文案。
        await static_quota.ensure_kb_token_quota(
            db,
            tenant_id=doc.tenant_id,
            kb_id=doc.kb_id,
            plan=plan,
            # 排除自己：重建一篇已在库里的文档时，它占的空间要算它自己的，
            # 否则库装满之后连重建都做不了（自锁，不是上限该有的行为）。
            excluding_document_id=doc.id,
            incoming=tokens,
        )

        # 切块器按**所属知识库**的配置现建：策略与窗口是库级属性，同一个库里
        # 混用两种切法会让片长不可比。
        splitter, split_strategy = await self._resolve_splitter(db, doc)
        pieces = splitter.split(parsed.content)
        if not pieces:
            raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "切块之后没有可用切片")

        # 节说明（可选增强）：按 section_id 去重后**每节一次**，不按片。
        # 关闭时 summarize_sections 直接返回空字典，检索文本与现状逐字一致。
        # 同节的正文按切片顺序累积给模型看——只给第一片会丢掉后半节。
        section_headings: dict[str, str] = {}
        section_texts: dict[str, list[str]] = {}
        for piece in pieces:
            section_id = str(piece.metadata.get("section_id") or "")
            if not section_id:
                continue
            section_headings.setdefault(section_id, str(piece.metadata.get("heading_path") or ""))
            section_texts.setdefault(section_id, []).append(piece.text)
        summaries = await summarize_sections(
            [
                (section_id, section_headings[section_id], "\n\n".join(texts))
                for section_id, texts in section_texts.items()
            ],
            title=doc.title,
        )

        retrieval_texts = [
            build_retrieval_text(
                doc.title,
                str(piece.metadata.get("heading_path") or ""),
                piece.text,
                section_context=summaries.get(str(piece.metadata.get("section_id") or "")),
                # 型号单独成段：`XR-2000` 与 `XR-2000 Pro` 只差一个后缀，埋在长句里
                # 时向量几乎分不开。实测型号题 hit@1 从 0.400 升到 0.600。
                # 行级表片例外（规则在 build_retrieval_text 里）：它的正文本来就是
                # `型号=XR-2100`，再加一段只是把问题关键词重复一遍，片越短占比越大。
                model_tags=True,
                chunk_type=str(piece.metadata.get("chunk_type") or ""),
            )
            for piece in pieces
        ]
        # 嵌入前再续一次：节说明与检索文本的构造也可能花掉几十秒（每节一次模型调用）。
        await self._heartbeat(doc, claim_token)
        vectors = await self._embeddings.embed_texts(retrieval_texts)
        if len(vectors) != len(retrieval_texts):
            raise ValueError(
                f"embedding count mismatch: {len(retrieval_texts)} texts -> {len(vectors)} vectors"
            )

        # 这一跑要写第几代（现场九·第二步）：在上一份产物上加一。必须在**构造切片之前**算好
        # （每一片都要带上它），也必须在 purge 之前——purge 之后旧切片就没了，再也读不出
        # 这一篇原来是第几代。
        # 用 ``getattr`` 读这一列：离线替身（``SimpleNamespace``）只镜像它关心的那几列，
        # 缺这一列时按「还没有任何一代产物」起算——``next_generation(None)`` 正好是第一代，
        # 与迁移回填的口径同一个号。生产路径上这一列永远存在（列可空，ORM 属性恒在）。
        generation = next_generation(getattr(doc, "current_index_generation", None))

        chunks: list[dict[str, Any]] = [
            {
                "id": new_chunk_id(),
                "tenant_id": doc.tenant_id,
                "kb_id": doc.kb_id,
                "document_id": doc.id,
                # content 是切片原文（落库 / 回给调用方）；retrieval_text 是检索文本
                # （进向量与关键词索引）。两者必须并存：向量存储只读 content，
                # 关键词 Provider 只读 retrieval_text。
                "content": piece.text,
                "retrieval_text": retrieval_texts[index],
                "embedding": vector,
                # 稿号与「当前稿」标记（#173 + #174）：两者都在**切片字典顶层**，不是 meta 里。
                #
                # ``doc_version``：这一片属于第几稿，落 ``chunks.doc_version`` 这个**真实列**。
                # 值来自上面解析出的 ``draft``（显式给了就用调用方那份，否则是当前稿指针）——
                # **不能用 ``doc.current_version`` 现读**：指针是活数据，迟到任务读到的会是
                # 别人的稿，从而写出「标着第 N 稿、装着别的稿正文」的切片。
                #
                # ``is_current``：关键词侧的「当前稿」标记，召回只用这一个 term 过滤。它是
                # 「这一片属于的稿 == 文档此刻的当前稿指针」，所以迟到任务写的旧稿切片自然是
                # ``False``——不参与召回，但仍留在库里（#173 的切片按稿保留）。
                "doc_version": draft,
                "is_current": draft == doc.current_version,
                "meta": {
                    # 解析器产出的 metadata 必须跟着切片走，否则「元数据.engine 含 ocr」
                    # 这类验收根本无从查起（本步实测踩过）：显式合并，切块字段优先。
                    **parsed.metadata,
                    **piece.metadata,
                    # 文档级序号。order_index 只在节内可比，而父节要跨 h3 拼回整个 h2，
                    # 没有它就排不出文档顺序（ADR-0010 的确定性要求）。
                    "chunk_index": index,
                    # 落库记下这一片是哪种策略切出来的：重建时才能复现同一套切法，
                    # 跨库检索时也解释得了片长差异。
                    "split_strategy": split_strategy,
                    # 节说明也落库：排查时能看出「这条检索文本为什么这么拼」，
                    # 重建时也不必重新调模型。
                    "section_context": summaries.get(str(piece.metadata.get("section_id") or "")),
                    # 页号由切块器从「第 N 页」标题带下来（PDF 才有）；Markdown 为空。
                    "page": piece.metadata.get("page"),
                    # **这里没有 doc_version**：稿号是 ``chunks.doc_version`` 那一列
                    # （见上面的顶层键）。JSONB 里再留一份就有两个来源，而 #172 的迁移
                    # 已经把存量行上那份删掉了——写入侧必须一起改，否则新行又把副本塞回来。
                    "title": doc.title,
                    # 原始文件名随切片落一份（#152 的修订）：Markdown 链接写的是文件名
                    # （``./points_policy.md``），而标题是《积分规则》——建边时按文件名才对得上。
                    # 落进 meta 而不是建边时回查 documents 表：meta 是**这一版**的快照，
                    # 事后改名不会让旧版本的边跟着变。
                    "source_filename": doc.source_filename or "",
                    "index": index,
                    "source_type": parsed.source_type,
                },
                # 这一片属于该文档的第几代**索引产物**（第 17 步现场九·第二步）。
                # 与 ``doc_version`` 正交：那个说「装的是哪一稿正文」，这个说「哪一次索引
                # 跑出来的」。检索侧只认「等于文档当前代」的那些片。
                "generation": generation,
            }
            for index, (piece, vector) in enumerate(zip(pieces, vectors))
        ]

        # Tx2：先删后写 + 建关系边 + 状态回写，一次提交；任一步失败都由调用方回滚。
        #
        # **开头先核对租约令牌**（#213）：Tx1 提交之后到这一刻之间，租约可能已经过期并
        # 被另一个任务接走（清扫器接活、或者重投的副本先跑完了）。此时这一笔写下去就是
        # 「僵尸任务覆盖新主人的结果」——它的切片会先 purge 掉对方的，再把旧内容写回来。
        # 令牌不等 → 整笔放弃：回滚（连正文回写一起撤销）、记一条 warning、返回 0。
        # 不抛可重试错误：这里不是失败，是「已经有人干了，我该退场」。
        #
        # 放在 purge **之前**是关键：检查完之前一行都没改。
        if not await document_repository.holds_index_claim_async(
            db, document_id, tenant_id=doc.tenant_id, token=claim_token
        ):
            # 先把要进日志的两列取出来：``rollback`` 会让 ORM 对象过期，之后再读属性会触发
            # 一次懒加载——异步会话里那是一次 ``MissingGreenlet``，而它会把「该退场」变成
            # 「任务崩了」。
            tenant_id, kb_id = doc.tenant_id, doc.kb_id
            await async_rollback_now(db)
            logger.warning(
                "indexing aborted, the lease was taken over by another worker",
                document_id=document_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
            )
            return 0

        # 清除按「这一稿」收窄：别的稿的切片一个字节都不动（#173，ADR-0005 D4 选 B）。
        await purge_document_chunks(
            db,
            document_id,
            tenant_id=doc.tenant_id,
            stores=self._stores,
            doc_version=draft,
            # 只清**这一代**（现场九·第五步）：重建写的是新代，上一代留着给回滚，由夜间
            # 保留策略（留最近 2 代）收口。关键词那一路不受这一维影响，它镜像当前代。
            generation=generation,
        )
        # ── 第四本账：每日切块额度（#222 / ADR-0035 决策三）──────────────
        #
        # 位置在**嵌入之前**：那一步才是花钱的。记的是**检索文本** token（真正发给嵌入模型的
        # 那一份：文档名 + 节标题路径 + 节说明前缀 + 正文），不是正文 token——后者是本库容量
        # 判据的口径。两个数字各管一件事，混用会让「容量没满但额度超了」解释不清。
        #
        # 超额抛 20037，让这一篇落失败（切片还没写、向量还没算），额度一分不扣：
        # 先判后记（ADR-0018），否则「拒绝之后额度更少了」会让用户永远恢复不了。
        await chunk_ledger.charge_chunk_tokens(
            db,
            tenant_id=doc.tenant_id,
            tokens=sum(token_count(chunk.get("retrieval_text") or "") for chunk in chunks),
            limit=plan.chunk_daily_tokens,
            plan_name=plan.name,
            plan_code=plan.code,
        )

        for store in self._stores:
            await store.add_chunks(db, chunks)

        # 版本行：这一稿已有就按需要改写它（重解析，#182），没有就补当前稿那一条（#175）。
        # 与切片、正文回写、状态同一笔事务。
        await self._record_version_content(
            db, doc, draft=draft, source=source, content=parsed.content
        )

        # 状态回写走一条显式 UPDATE（#212），并**检查它影响了几行**：库里这一行若已被删
        # （或租户对不上），写得「成功」而实际一行没动正是本票要堵的那类静默失效。
        # 正文 token 数：容量判据的唯一计量。与切片、状态同一笔事务——半份状态（切片写了
        # 而计量没写）会让下一次容量判定按一个偏小的数放行。
        await document_repository.update_token_count(
            db, document_id, tenant_id=doc.tenant_id, token_count=tokens
        )
        # 指针推进与指纹回写必须与切片同一笔事务：分开写会出现「切片已经是第 3 代、文档还
        # 指着第 2 代」的中间态，而那段时间这一篇在检索里是**查不到的**（按代数对齐不上）。
        await document_repository.advance_index_generation(
            db,
            document_id,
            tenant_id=doc.tenant_id,
            generation=generation,
            settings_hash=await knowledge_base_repository.get_settings_fingerprint(
                db, doc.kb_id, tenant_id=doc.tenant_id
            ),
        )
        written = await document_repository.update_status(db, doc, DocumentStatus.SUCCESS)
        if written != 1:
            logger.warning(
                "document status write touched no row",
                document_id=document_id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
                written=written,
            )
        await async_commit_now(db)
        logger.info(
            "document indexed",
            document_id=document_id,
            tenant_id=doc.tenant_id,
            kb_id=doc.kb_id,
            chunk_count=len(chunks),
            token_count=tokens,
            generation=generation,
        )
        return len(chunks)

    async def _record_version_content(
        self,
        db: AsyncSession,
        doc: Document,
        *,
        draft: int,
        source: _ContentSource,
        content: str,
    ) -> None:
        """把这次索引用的正文落到版本表：**有这一稿就改写它，没有就补当前稿那一条**。

        两条规则的边界是「这次是不是重新解析出来的正文」：

        * ``source.reparsed`` 且这一稿已有版本行 → **改写同一行**（正文 + 校验和，
          :func:`app.repositories.document_version_repository.rewrite_content`）。
          重解析不是改稿：同一份原始文件换了个解析器再读一遍，稿号不变、不新增行、
          不动指针与归档状态（#182）。不这么做的话，文档行被改写而版本行留着上一份
          解析产物——「正文真值在版本表」这条不变量当场破，T10 的对账永远红；
        * 否则这一稿还没有版本行 → :meth:`_ensure_version_row` 补（#175），且只补
          当前稿：给一个迟到任务的旧稿号凭空建行等于在时间轴上编造一稿。

        非重解析的那两支（正文来自版本表 / 文档行）什么都不用写：版本行本来就是正文
        真值，重算一遍校验和既无必要，也会把「存量行的校验和是怎么来的」悄悄改写。
        """

        if source.version_row is not None:
            if source.reparsed:
                await document_version_repository.rewrite_content(
                    db, source.version_row, content=content
                )
            return
        await self._ensure_version_row(db, doc, draft=draft, content=content)

    async def _ensure_version_row(
        self, db: AsyncSession, doc: Document, *, draft: int, content: str
    ) -> None:
        """「每篇成功文档恰好一条当前稿版本行」——缺了就在这一笔事务里补上（#175）。

        ## 为什么必须补

        上传链路只登记文档、不建版本行（``app.services.document_service.upload`` 的职责就是
        「登记 + 投递」）。#172 之前上传的存量文档靠迁移回填过版本行，而**#172 之后新上传的
        文档**两边都没有——于是「版本表里有这一稿的正文」这条不变量对它们不成立，
        「当时引用的是哪一版」自然也就没有答案。补在**索引成功**这一刻，是因为此时才第一次
        有了确切的正文（文件上传的正文是解析出来的，上传那一刻还没有）。

        ## 为什么只补当前稿

        ``draft != doc.current_version`` 时一律不补：迟到的**重解析**任务会带着旧稿号跑完，
        给它建行等于在时间轴上编造一稿——正文是现在重解析出来的那一份，稿号却是旧的。
        （#182 起，这种任务若那一稿**已有**版本行，产物是写回那一行
        ——见 :meth:`_record_version_content`；这里只负责「那一稿还一行都没有」的情形。）

        ## 为什么不在这里无条件收紧 ``_content_of``

        「没有版本行就报错」会打挂上传链路（刚上传的文档本来就没有版本行）。读路径保持
        「版本行优先、没有就回落文档行」，写路径负责把缺的那一条补上——一松一紧各管一段。

        ## 正文为什么原样存（不在这里规范化）

        版本行的正文是**重建时会读到**的那份文本（ADR-0005 D6）。规范化会削掉首尾空行、
        换掉换行符，于是「重建一次」切出的切片与当初那次不再逐字相同——而校验和本身
        （:func:`app.utils.checksum.checksum_of`）内部已经规范化，判重不受影响。
        """

        if draft != doc.current_version:
            return
        existing = await document_version_repository.get_by_version(db, str(doc.id), draft)
        if existing is not None:
            return
        await document_version_repository.create(
            db,
            DocumentVersion(
                id=new_version_id(),
                document_id=str(doc.id),
                version=draft,
                content=content,
                source_file_path=doc.source_file_path,
                checksum=checksum_of(content),
                status=DOCUMENT_VERSION_CURRENT,
            ),
        )
