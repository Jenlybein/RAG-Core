"""文档数据访问：创建、状态回写、索引抢占（CAS）与租约判定。

并发去重靠数据库，不靠 Celery：``try_claim_for_indexing`` 是一条
``UPDATE ... WHERE``，两个 Worker 同时抢时，Postgres 的行锁 + READ COMMITTED
重评估会让后到者 ``rowcount=0``，于是它直接跳过。

**索引权的归属只有一处**：入队侧（上传 / 重建 / 建版本 / 回滚 / 两个运维脚本）只调用
:func:`prepare_for_indexing` 把文档置成「已排队、未开工」（``status=QUEUED``、
``index_claimed_at=NULL``，不盖租约）；Worker 才是唯一的抢占者，调用
:func:`try_claim_for_indexing` 盖租约（``status=PROCESSING`` + 抢占时间）。
若入队侧也抢先盖租约，Worker 的 WHERE 会因「租约新鲜」而拒绝它，重建与重试都会
变成静默 no-op。

**「谁在跑」只由租约回答**（ADR-0034 决策一）：``status=PROCESSING`` 只是「Worker 抢占时
盖的章」，过期之后它不再说明任何事；:func:`is_lease_alive` 是这条判定的纯函数版本，
删除 / 重建 / 建版本 / 回滚的守卫都读它。

## 异步孪生（第 11 步票 #132 / #130）

入库与索引链路（上传登记、Worker 抢占 / 回写 / 释放租约）与控制面（树、重建、删除、读原文）
都走 ``AsyncSession``，所以本模块的 ``*_async`` 孪生覆盖这两条路径上的全部读与写（后缀与
检索链路的 ``get_by_id_for_tenant`` / ``fetch_meta_by_chunk_ids`` 一致）。
**语句本身只写一遍**（``_claim_statement`` / ``_release_statement`` / ``_prepare_statement`` /
``_status_statement`` / 两个计数的 statement builder），同步与异步两个入口执行的是同一条
SQL——CAS 的判定条件与租约语义因此不可能在两套入口之间漂移。

## 状态回写是**语句**，不是「改对象属性」（#212）

:func:`update_status` 执行一条显式 ``UPDATE``（带 ``tenant_id`` 过滤、同时抬
``updated_at``），并把 ``rowcount`` 交给调用方。曾经的写法是给 ORM 对象赋属性再
``flush``，它依赖脏检查——而抢占是一条 ``synchronize_session=False`` 的语句、会话又是
``expire_on_commit=False``，于是「把属性赋成本来就是的那个值」不产生任何 UPDATE：整库构建
跑完之后，库里的状态与 ``updated_at`` 永远停在抢占那一刻，而切片与向量都在。语句不猜，
写没写进去由 ``rowcount`` 说。

## 抢占带令牌（#213）

:func:`try_claim_for_indexing` 抢到时**返回它实际写进库的那个时间戳**（没抢到是
``None``）：这个值就是租约令牌，调用方拿着它去 Tx2 开头核对
（:func:`holds_index_claim`）。不让调用方自己再读一次时间：那样读到的与写进库的会有微秒级
差，令牌永远对不上。令牌挡住的是「租约过期被后续任务接走、旧任务稍后又醒来」——
僵尸任务醒来时看到的已经是别人的时间戳，整笔放弃。

同步的那几个**仍有读者，但不与上面的异步路径共用**：运维脚本
（``reindex_knowledge_base`` / ``requeue_stuck_documents`` /
``backfill_document_content``）与启动期装载是独立进程 / 线程池里的同步读；
``get_by_id_for_tenant`` / ``list_by_kb``
两个同步孪生现在只剩测试在用（生产零调用点），留着是为了让状态机的 SQL 级用例能直接
读行，不作为第二套生产写法。
"""

from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import ColumnElement, and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import Update

from app.db.base import now
from app.models.document import Document
from app.utils.status import DocumentStatus

# 索引租约秒数（运维策略，不是部署配置）：超过这个时长仍停在 PROCESSING 的文档
# 视为「Worker 卡死」，允许被重新抢占 / 删除 / 重建。否则 Worker 崩一次就永久
# 锁死——正是教程里「永远卡在处理中且不报错」的坑。
# 窗口长度按「最慢的一篇也不至于被判死」定；任务执行期间靠心跳在窗口内续租
# （ADR-0034 决策七），所以长文档不会被误判成死。
DOCUMENT_LEASE_SECONDS = 600


def get_by_id_for_tenant(db: Session, document_id: str, *, tenant_id: str) -> Document | None:
    """按 ``id`` **且** ``tenant_id`` 取文档，缺失与跨租户一律返回 ``None``。

    ADR-0006（不做文档级 ACL，隔离只到租户 + Key 的数据范围）要求所有按 id 取
    文档的数据面路径都强制带租户过滤，调用方拿到 ``None`` 后统一按
    ``NOT_FOUND``（20016）处理，不泄露存在性。租户是无默认值的强制参数，不是
    可选过滤器；无租户的那一条（:func:`get_by_id`，异步）只留给 Worker 任务路径。
    """

    stmt = select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    return db.execute(stmt).scalar_one_or_none()


def list_by_kb_and_status(
    db: Session, kb_id: str, *, tenant_id: str, status: int
) -> list[Document]:
    """该租户在该知识库下、处于指定状态的文档，按创建时间升序。

    运维脚本「全知识库重建」（``scripts/reindex_knowledge_base.py``）用它取出所有
    ``SUCCESS`` 的文档；``tenant_id`` 同样是强制参数（ADR-0006），脚本在扫描时
    显式传入，绝不跨租户。
    """

    stmt = (
        select(Document)
        .where(
            Document.kb_id == kb_id,
            Document.tenant_id == tenant_id,
            Document.status == status,
        )
        .order_by(Document.created_at)
    )
    return list(db.execute(stmt).scalars().all())


def list_stuck(
    db: Session,
    *,
    tenant_id: str | None = None,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    kb_id: str | None = None,
) -> list[Document]:
    """扫出「卡死」的文档：排队中，或 ``PROCESSING`` 且租约缺失或已过期。

    运维扫描：显式跨租户；只有 ``scripts/requeue_stuck_documents.py`` 调用，
    数据面禁止使用。``tenant_id`` 一旦给出就强制过滤。

    判定与 CAS 的可抢占条件完全一致（ADR-0034 决策一）：

    * ``status=QUEUED``：已经收下但没有 Worker 接。任务丢了一条消息（Broker 丢包、
      Worker 崩在 ``ack`` 之前）之后，这一行永远不会自己动，只有这里能把它重新送出去。
      重投是**幂等**的——迟到的重复任务在抢占时会被 CAS 挡掉（``rowcount=0``），
      不会多切一遍。
    * ``status=PROCESSING`` 且 ``index_claimed_at IS NULL``：迁移期兼容形状，等价于排队中。
    * ``status=PROCESSING`` 且租约早于 ``now - lease_seconds``：Worker 崩了 / 卡住。

    有效租约内的处理中不算卡死，不能被这条查询救回。``kb_id`` 只缩小范围，
    不改变「卡死」的定义；缺省扫全部知识库（``tenant_id`` 缺省为 None，仅有
    运维权限的本地脚本会这样用）。
    """

    lease_before = now() - timedelta(seconds=lease_seconds)
    stmt = select(Document).where(
        or_(
            Document.status == DocumentStatus.QUEUED,
            and_(
                Document.status == DocumentStatus.PROCESSING,
                or_(
                    Document.index_claimed_at.is_(None),
                    Document.index_claimed_at < lease_before,
                ),
            ),
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(Document.tenant_id == tenant_id)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    return list(db.execute(stmt.order_by(Document.created_at)).scalars().all())


class MissingContentDocument(NamedTuple):
    """「正文为空」的一行候选：**只有补齐脚本真正需要的列**（无 ORM 实体）。

    为什么不是 ``Document`` 实体：候选查询必须能在**升级前**跑（见
    :func:`list_missing_content` 的纪律），而实体映射会自然演化——#171 那次改名
    （``doc_version`` → ``current_version``）之后，``select(Document)`` 就要求老库里
    存在一列还不存在的列，于是「升级前先补正文」这条路径在升级前直接崩掉。具名列集把
    「这次查询依赖哪些列」写成可读的清单，加列的人必须来这里显式加一行并为此负责。

    ``content`` 与 ``source_*`` 一并取回：脚本要靠它们判断「这篇有没有正文」，以及
    「源文件在哪、按什么名字解析」。
    """

    id: str
    tenant_id: str
    kb_id: str
    content: str | None
    source_type: str | None
    source_file_path: str | None
    source_filename: str | None


#: 候选查询的列集（顺序与 :class:`MissingContentDocument` 的字段一一对应）。
#:
#: **不许**出现第 12 步（文档版本管理）之后才存在的列——``current_version`` 是典型反例：
#: 老库上它还不存在，而这条查询恰恰要在升级前跑。``created_at`` 可以用（老库就有），
#: 它只出现在 ``ORDER BY`` 里，不进这份清单。
_MISSING_CONTENT_COLUMNS = (
    Document.id,
    Document.tenant_id,
    Document.kb_id,
    Document.content,
    Document.source_type,
    Document.source_file_path,
    Document.source_filename,
)


def list_missing_content(
    db: Session, *, kb_id: str | None = None, limit: int | None = None
) -> list[MissingContentDocument]:
    """扫出「正文为空」的文档：正文回写之前登记的历史行（第 12 步票 #170）。

    ## 这条查询的纪律（ADR-0006「第 1 次修订」）

    * **仅运维脚本使用**：唯一调用方是 ``scripts/backfill_document_content.py``；
    * **跨租户是刻意的**：它没有 ``tenant_id`` 参数（连可选都没有）——「库里还有哪些文档
      没有正文」是整库的一次性运维问题，按租户切分会让别的租户里缺正文的文档永远补不上；
    * **数据面禁用**：检索 / 列表 / 管控接口一律不得调用它；那些路径上的强制租户过滤
      不能省，也不得拿这一条当先例。

    ## 第二条纪律：**这条查询必须能在升级前跑**

    脚本的使命是「先补正文，再跑迁移」（迁移会拦「有切片却正文为空」的文档，并把正文
    抄进版本行）。所以这次查询**永远不许引用迁移之后才存在的列**——它跑在迁移**之前**，
    那些列那时还不存在。``select(Document)`` 正是一次反面教材：实体加了
    ``current_version``（#171 改名）之后，这条查询在老库上直接
    ``UndefinedColumn: column documents.current_version does not exist``，而它本来该在
    升级前正常工作。

    于是这里显式列选择（:data:`_MISSING_CONTENT_COLUMNS`），返回具名的
    :class:`MissingContentDocument`。**将来要给这条查询加列的人，请先回答「老库上有没有
    这一列」**：迁移之后才有的列一律不加，需要它的信息就在脚本里按 id 二次读（并接受
    「升级后拒跑」这条守卫）。``tests/test_document_content_backfill.py`` 用**只有老列**的
    表结构钉住这条纪律。

    判定是 ``content`` 为空串或 ``NULL``：历史行两种都可能（建表时没有默认值的那批
    是 ``NULL``，文件上传的那批是空串）。``kb_id`` 只缩小范围，``limit`` 供运维先看
    一小批再决定，两者都不改变「正文为空」的定义。
    """

    stmt = select(*_MISSING_CONTENT_COLUMNS).where(func.coalesce(Document.content, "") == "")
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    stmt = stmt.order_by(Document.created_at)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [MissingContentDocument(*row) for row in db.execute(stmt).all()]


def set_content(db: Session, document_id: str, content: str) -> None:
    """把解析出的正文写回 ``documents.content``（#170；只 execute + flush，不提交）。

    为什么是 ``UPDATE`` 语句而不是 ``candidate.content = ...``：候选查询刻意不交出 ORM
    实体（见 :func:`list_missing_content`），写回也就不该把实体请回来——这条路径要能在
    升级前跑，而实体写回意味着「整行映射都在前提里」。写哪一行由 ``document_id`` 定，
    正文由调用方给，不经过 ORM 的脏检查。

    提交点纪律：这里**不** commit。补齐脚本一篇一笔事务（一篇失败不影响其余），提交边界
    由它掌握，与整个仓库「写入不带提交」的分工一致。
    """

    db.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(content=content)
        .execution_options(synchronize_session=False)
    )
    db.flush()


async def list_with_expired_versions(
    db: AsyncSession, *, retain: int, kb_id: str | None = None, limit: int | None = None
) -> list[Document]:
    """扫出「保留窗口可能非空」的文档：当前稿号 > 保留稿数（第 12 步票 #178）。

    ## 这条查询的纪律（ADR-0006「第 1 次修订」）

    * **仅运维脚本使用**：唯一调用方是 ``scripts/purge_document_versions.py``；
    * **跨租户是刻意的**：它没有 ``tenant_id`` 参数——清理要问的是「整库里谁的保留窗口
      非空」，按租户切分只会让一次清理跑成 N 次、且漏掉运维没想到的那些租户；
    * **数据面禁用**：检索 / 列表 / 管控接口一律不得调用它（也不得拿它当先例）。

    窗口下界是 ``当前稿号 - retain``，所以只有当它 >= 1 时才可能有稿子落在窗口外——
    一篇只长到第 3 稿、窗口又留 3 稿的文档不必被扫出来，``>=`` 会让绝大多数文档白跑。

    判定用的是**指针**（``documents.current_version``）而不是版本行的 ``max``：窗口按稿数
    从指针算起是业务定义（D10），两份真值不一致时行为要可预期，而不是随 ``max`` 漂移。

    它这里是**异步**的，与 :func:`list_missing_content` 不同：清理必须走
    ``purge_document_chunks``，那条只接受 ``AsyncSession``（异步栈第 11 步起），脚本于是
    整条链都在异步会话上。
    """

    stmt = select(Document).where(Document.current_version > retain)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    stmt = stmt.order_by(Document.created_at, Document.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_for_version_reconcile(
    db: AsyncSession, *, kb_id: str | None = None, limit: int | None = None
) -> list[Document]:
    """扫出要参与**文档版本对账**的文档（第 12 步票 #179）。

    ## 这条查询的纪律（ADR-0006「第 1 次修订」）

    * **仅运维脚本使用**：唯一调用方是 ``scripts/verify_document_versions.py``；
    * **跨租户是刻意的**：它没有 ``tenant_id`` 参数——对账的对象是整库的存量数据，
      按租户切分会让「另一个租户里的分歧」永远看不见（这正是对上账票存在的意义）；
    * **数据面禁用**：检索 / 列表 / 管控接口一律不得调用它。

    关键词那一维在读的时候**照旧逐租户传**（ADR-0006 的强制过滤在那儿不能省）：
    这条扫描只交出「要对账哪些文档」，拿文档 id 去读内容的那一步仍然带租户。

    ``created_at, id`` 双键排序：同一秒建出来的几篇文档之间也要有**确定**的顺序，
    否则 ``--limit`` 两次跑出来的那两篇可能不是同一批（运维据此定位，不是抽奖）。
    """

    stmt = select(Document)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    stmt = stmt.order_by(Document.created_at, Document.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


def list_by_kb(db: Session, kb_id: str, *, tenant_id: str) -> list[Document]:
    """该租户在该知识库下的全部文档，按创建时间升序（列表即轮询进度的时间线）。

    ``tenant_id`` 是强制参数，不是可选过滤器：漏掉它就会跨租户泄露文档（ADR-0006
    要求 Repository 方法签名强制带租户）。这里不提供无租户的变体。
    """

    stmt = (
        select(Document)
        .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
        .order_by(Document.created_at)
    )
    return list(db.execute(stmt).scalars().all())


def _count_by_kb_statement(kb_id: str, *, tenant_id: str):
    """「这个库有多少份文档」的那一条计数（同步与异步孪生共用）。"""

    return (
        select(func.count())
        .select_from(Document)
        .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
    )


def _claimable_conditions(
    lease_before: datetime, *, allow_success: bool
) -> list[ColumnElement[bool]]:
    """可抢占的行条件（纯条件，不含语句）：抢占与「准备入队」共用同一份。

    ADR-0034 决策一的**唯一**一处定义：

    * ``4 排队中``：被收下、没人接——正常路径；
    * ``3 且租约为空或已过期``：迁移期兼容 + Worker 崩了 / 卡住（租约过期等义于排队中）；
    * ``2 失败``：失败可重试；
    * ``allow_success``：构建形状额外允许重建一篇已经成功的文档（ADR-0034 决策二）。

    抽成一处，防止入队与抢占两个函数的判定漂移——漂移的症状是「重建静默 no-op」或
    「同一篇被两个 Worker 各切一遍」。
    """

    claimable: list[ColumnElement[bool]] = [
        Document.status == DocumentStatus.QUEUED,
        Document.status == DocumentStatus.FAILED,
        and_(
            Document.status == DocumentStatus.PROCESSING,
            or_(
                Document.index_claimed_at.is_(None),
                Document.index_claimed_at < lease_before,
            ),
        ),
    ]
    if allow_success:
        claimable.append(Document.status == DocumentStatus.SUCCESS)
    return claimable


def _prepare_statement(
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int,
    allow_success: bool,
    prepared_at: datetime,
) -> Update:
    """「准备入队」那条 ``UPDATE``：同步与异步两个入口执行的是同一条语句。

    写的是 ``status=QUEUED``（不是 PROCESSING）：入队侧只说「已收下、排队等 Worker」，
    真正在跑的印章由 Worker 在抢占时盖。三态时代这里写 3 却清租约，等于用同一个字面值
    表达「排队」和「在跑」两件事——那正是这次要拆掉的混淆。
    """

    lease_before = prepared_at - timedelta(seconds=lease_seconds)
    return (
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            or_(*_claimable_conditions(lease_before, allow_success=allow_success)),
        )
        .values(
            status=DocumentStatus.QUEUED,
            index_claimed_at=None,
            error_message=None,
            updated_at=prepared_at,
        )
        .execution_options(synchronize_session=False)
    )


def prepare_for_indexing(
    db: Session,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """入队侧：把可重建的文档标记为「排队中」；``rowcount > 0`` 即成功。

    与 :func:`try_claim_for_indexing` 用同一份可重建条件，但**不盖租约**：
    ``index_claimed_at=NULL`` 才允许 Worker 之后抢占。这里只做状态准备（
    ``status=QUEUED``、清 ``error_message``、抬 ``updated_at``），真正的
    索引权由 Worker 唯一负责。``tenant_id`` 是强制过滤参数（ADR-0006）。
    """

    stmt = _prepare_statement(
        document_id,
        tenant_id=tenant_id,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
        prepared_at=now(),
    )
    return int(db.execute(stmt).rowcount or 0) > 0


def try_claim_for_indexing(
    db: Session,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> datetime | None:
    """Worker 侧的单条 ``UPDATE ... WHERE`` 抢占索引权；抢到回**租约令牌**，没抢到回 ``None``。

    可抢占的条件全部写在同一个 WHERE 里（见 :func:`_claimable_conditions`）；
    抢到即写入 ``status=PROCESSING`` 与新的抢占时间，并清掉上一次的
    ``error_message``。``tenant_id`` 是强制过滤参数（ADR-0006）。
    调用方提交事务后立刻释放行锁，不要在这里包住 embedding。

    返回值是**这一条语句实际写进库的** ``index_claimed_at``（#213）：它就是租约令牌，
    写切片那一笔事务的开头用 :func:`holds_index_claim` 核对它。返回 ``bool`` 曾让调用方
    只能知道「抢到了」，核对时只能重新读一次时间——那一次读与这里的写之间就有一道缝。

    Worker 一律用 ``allow_success=False``：重建端点已经用
    :func:`prepare_for_indexing` 把 SUCCESS 文档置成 QUEUED，ACK 丢失导致的
    迟到重复投递必须抢不到，否则会在 SUCCESS 之后重写切片。
    """

    claimed_at = now()
    stmt = _claim_statement(
        document_id,
        tenant_id=tenant_id,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
        claimed_at=claimed_at,
    )
    return claimed_at if int(db.execute(stmt).rowcount or 0) > 0 else None


def _claim_statement(
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int,
    allow_success: bool,
    claimed_at: datetime,
) -> Update:
    """抢占索引权的那一条 ``UPDATE``（同步与异步孪生共用，见模块文档「异步孪生」）。

    ``claimed_at`` 由调用方传入而不是在这里现取：它就是抢占函数返回给调用方的租约令牌，
    语句里写下的值与返回的值必须是**同一个对象**，否则令牌核对会因为微秒级差异而永远不等。
    """

    lease_before = claimed_at - timedelta(seconds=lease_seconds)
    return (
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            or_(*_claimable_conditions(lease_before, allow_success=allow_success)),
        )
        .values(
            status=DocumentStatus.PROCESSING,
            index_claimed_at=claimed_at,
            error_message=None,
            updated_at=claimed_at,
        )
        .execution_options(synchronize_session=False)
    )


def _claim_token_statement(document_id: str, *, tenant_id: str):
    """Tx2 开头核对租约令牌的那一条 ``SELECT ... FOR UPDATE``（同步与异步孪生共用）。

    ``FOR UPDATE`` 不是为了防别的写者（那由令牌判定负责），而是为了在读到令牌之后
    到这一笔提交之前，没人能改掉这一行的租约：没有行锁时，另一个 Worker 可以在
    核对的下一秒把行抢走，而这一笔仍然会写切片——令牌核对就成了「检查完就过期」。
    """

    return (
        select(Document.index_claimed_at)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .with_for_update()
    )


def release_index_claim(db: Session, document_id: str, *, tenant_id: str) -> None:
    """释放索引租约：``index_claimed_at=NULL``，``PROCESSING`` 归一成 ``QUEUED``。

    可重试失败在 ``self.retry`` 之前调用它：不释放的话，重试的下一次
    :func:`try_claim_for_indexing` 会看到仍然新鲜的租约而抢不到，重试永远不执行。

    ``3 → 4`` 的归一不是语义要求（「``3`` 且租约为空」本来就等义于排队中），而是
    实现选择（ADR-0034 决策一「清扫器重投」那一行）：把行写成它实际所处的状态，
    界面与「在跑」的判据才不会依赖一条过期租约。**只归一 ``PROCESSING``**——
    ``FAILED`` / ``SUCCESS`` 上的租约清理不该顺手改状态，那是另一件事的终态。
    ``tenant_id`` 是强制过滤参数（ADR-0006）。
    """

    db.execute(_release_statement(document_id, tenant_id=tenant_id))


def _release_statement(document_id: str, *, tenant_id: str) -> Update:
    """释放租约的那一条 ``UPDATE``（同步与异步孪生共用）。"""

    return (
        update(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .values(
            index_claimed_at=None,
            status=case(
                (Document.status == DocumentStatus.PROCESSING, DocumentStatus.QUEUED),
                else_=Document.status,
            ),
        )
        .execution_options(synchronize_session=False)
    )


def is_lease_alive(document: Document, *, lease_seconds: int = DOCUMENT_LEASE_SECONDS) -> bool:
    """纯函数：这份文档是否仍处于「有效处理中」（=此刻真的有人在跑）。

    **只看 ``PROCESSING``**（ADR-0034 决策一）：``QUEUED`` 没有租约、也不该有，因此
    永远不是「在跑」；排队中的文档可以删除（撤回排队）、可以重新提交（替换排队）。
    ``PROCESSING`` 还要求 ``index_claimed_at`` 在 ``[now - lease_seconds, now]`` 窗口内；
    ``index_claimed_at is None``（迁移期兼容形状）与租约过期（Worker 卡死）都不算，
    因此允许被删除 / 重建 / 重新抢占。
    """

    if document.status != DocumentStatus.PROCESSING:
        return False
    if document.index_claimed_at is None:
        return False
    return document.index_claimed_at >= now() - timedelta(seconds=lease_seconds)


# ──────────────── 异步孪生（第 11 步票 #132 / #130：入库索引与控制面）
#
# 每一条都与上面的同步版**执行同一条语句**（共用 statement builder），差别只有「等不等」。
# 同步版只剩两个运维脚本在用，删除条件见模块文档。


async def create(db: AsyncSession, doc: Document) -> Document:
    """登记一份文档（只 flush，不提交——提交点是 ``app.db.transaction``）。"""

    db.add(doc)
    await db.flush()
    return doc


async def get_by_id(db: AsyncSession, document_id: str) -> Document | None:
    """按主键取，**不带租户过滤**：只给 Worker 任务用（同步的那一份已随 #134 删除）。"""

    document: Document | None = await db.get(Document, document_id)
    return document


async def get_by_id_for_tenant_async(
    db: AsyncSession, document_id: str, *, tenant_id: str
) -> Document | None:
    """按 id + 租户取（语义同同步版 :func:`get_by_id_for_tenant`；控制面与数据面都走它）。"""

    stmt = select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_by_kb_async(db: AsyncSession, kb_id: str, *, tenant_id: str) -> list[Document]:
    """该租户在该知识库下的全部文档（语义同 :func:`list_by_kb`，控制面树与数据面列表）。"""

    stmt = (
        select(Document)
        .where(Document.kb_id == kb_id, Document.tenant_id == tenant_id)
        .order_by(Document.created_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def update_token_count(
    db: AsyncSession, document_id: str, *, tenant_id: str, token_count: int
) -> int:
    """把这一篇的正文 token 数写进行，返回影响的行数（#220）。

    ## 为什么是语句而不是「改对象属性」

    与 ``update_status`` 同一条纪律（#212 的教训）：抢占、入队准备都是**语句**改库，而会话
    又是 ``expire_on_commit=False``——内存对象可能停留在旧值上。用语句写，这一列的真相
    就只有库里那一份，也不会有「赋成本来就是的值被 ORM 吞掉」这种静默失败。

    ## 为什么返回值被检查

    一行都没动 = 这篇文档在跑的过程中被删了 / 租户对不上。那时「写成功了」是个假象，而
    容量判据会按一个偏小的总量放行下一次上传。
    """

    statement = (
        update(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .values(token_count=max(int(token_count), 0))
        .execution_options(synchronize_session=False)
    )
    return int((await db.execute(statement)).rowcount or 0)


async def update_status(
    db: AsyncSession, doc: Document, status: int, error_message: str | None = None
) -> int:
    """回写状态与错误文案：执行一条显式 ``UPDATE``（只 flush，不提交），返回影响行数。

    **为什么是语句而不是「改对象属性」**（#212）：见模块文档。一句话——抢占是
    ``synchronize_session=False`` 的语句、会话``expire_on_commit=False``，于是内存里那份
    文档可能落后于库；此时把属性赋成本来就有的值，ORM 判定无脏属性，一条 UPDATE 都不产生，
    库里的状态就永远停在「索引中」。

    返回 ``rowcount``：它是「真的写了」的唯一证据，调用方据此发现「没写进去」
    （行在写之前被删掉了、``tenant_id`` 对不上）。``updated_at`` 与状态同一条语句抬，
    于是「更新时间晚于抢占时刻」这件事在库里可查。

    ``tenant_id`` 取自传入对象的租户（ADR-0006 要求写路径带租户过滤）；签名保持不变，
    调用点一个都不用改。
    """

    result = await db.execute(
        _status_statement(
            str(doc.id),
            tenant_id=doc.tenant_id,
            status=status,
            error_message=error_message,
            updated_at=now(),
        )
    )
    return int(result.rowcount or 0)


def _status_statement(
    document_id: str,
    *,
    tenant_id: str,
    status: int,
    error_message: str | None,
    updated_at: datetime,
) -> Update:
    """状态回写的那一条 ``UPDATE``（同步与异步孪生共用；目前只有异步入口在执行）。

    ``updated_at`` 显式写在 values 里而不是交给列的 ``onupdate``：这一条语句的全部意义
    就是「库里这一行确实被写了」，时间戳跟着一起写死才不会在某种方言 / 选项下静默缺一半。
    """

    return (
        update(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .values(status=status, error_message=error_message, updated_at=updated_at)
        .execution_options(synchronize_session=False)
    )


async def prepare_for_indexing_async(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> bool:
    """入队侧的状态准备（语义同 :func:`prepare_for_indexing`，控制面重建走它）。"""

    stmt = _prepare_statement(
        document_id,
        tenant_id=tenant_id,
        lease_seconds=lease_seconds,
        allow_success=allow_success,
        prepared_at=now(),
    )
    return int((await db.execute(stmt)).rowcount or 0) > 0


async def try_claim_for_indexing_async(
    db: AsyncSession,
    document_id: str,
    *,
    tenant_id: str,
    lease_seconds: int = DOCUMENT_LEASE_SECONDS,
    allow_success: bool,
) -> datetime | None:
    """Worker 侧抢占索引权；抢到回租约令牌，没抢到回 ``None``。

    语义与返回值都同 :func:`try_claim_for_indexing`（同一个 statement builder、同一个令牌）。
    """

    claimed_at = now()
    result = await db.execute(
        _claim_statement(
            document_id,
            tenant_id=tenant_id,
            lease_seconds=lease_seconds,
            allow_success=allow_success,
            claimed_at=claimed_at,
        )
    )
    return claimed_at if int(result.rowcount or 0) > 0 else None


async def holds_index_claim_async(
    db: AsyncSession, document_id: str, *, tenant_id: str, token: datetime
) -> bool:
    """这一行的租约是否**仍然是我抢到的那一次**（#213 的 fencing）。

    写切片那一笔事务的**开头**调用它：``SELECT ... FOR UPDATE`` 锁住文档行并读出
    ``index_claimed_at``，与手里的令牌比对。不等就是「租约已经过期、被后续任务接走了」，
    调用方必须整笔放弃——僵尸任务醒来时写的每一行切片都会盖掉新主人的结果，而它自己的
    状态回写还会把文档从「索引中」改回「成功」，症状是「重建完了，检索内容却是旧的」。

    行不存在（文档被删）同样回 ``False``：没有租约可以属于我，也就不该往库里写切片。

    ``FOR UPDATE`` 的理由见 :func:`_claim_token_statement`。``token`` 是
    :func:`try_claim_for_indexing_async` 返回的**那个对象**，不是重新取的时间。
    """

    current = (
        await db.execute(_claim_token_statement(document_id, tenant_id=tenant_id))
    ).scalar_one_or_none()
    return current is not None and current == token


async def release_index_claim_async(db: AsyncSession, document_id: str, *, tenant_id: str) -> None:
    """释放索引租约（语义同 :func:`release_index_claim`）。"""

    await db.execute(_release_statement(document_id, tenant_id=tenant_id))


async def count_tokens_in_kb(
    db: AsyncSession,
    kb_id: str,
    *,
    tenant_id: str,
    excluding_document_id: str | None = None,
) -> int:
    """这个库的**正文 token 总量**（容量判据，ADR-0035 决策四）。

    ``excluding_document_id`` 排除某一篇：重建一篇已在库里的文档时，它自己占的空间要算它
    自己的——不排除它会形成自锁（库装满之后连重建都做不了，而那是个功能倒退，不是上限
    该有的行为）。

    为什么数 ``SUM`` 而不是「把文档读出来在 Python 里累加」：几百篇长文档的正文可能有几十
    MB，读回来只为了求和是浪费；而且这件事本来就该由数据库回答。
    """

    conditions = [
        Document.kb_id == kb_id,
        Document.tenant_id == tenant_id,
    ]
    if excluding_document_id is not None:
        conditions.append(Document.id != excluding_document_id)
    statement = select(func.coalesce(func.sum(Document.token_count), 0)).where(*conditions)
    # ``scalar_one_or_none`` 而不是 ``scalar_one``：真库上聚合**总是**回一行（无行时是 NULL），
    # 但替身会话按「预置的行数」回答聚合，空预置就回 0 行——那时两边的语义必须一致才能
    # 用同一份用例跑「库还没装东西」这条路径。NULL / 无行都按 0 读。
    #
    # **两种收尾形状**：真库回一个数（``SUM``）；替身会话把「预置的那一行」原样交回来，
    # 于是一个对象也可能出现在这里。就对它读 ``token_count``（那一行本来就是文档行），
    # 读不到就按 0——替身那条路本来就没在算聚合，它只是不让容量判据把不相关的用例带崩。
    value = (await db.execute(statement)).scalar_one_or_none()
    if isinstance(value, (int, float)):
        return int(value)
    return int(getattr(value, "token_count", 0) or 0)


async def count_by_kb(db: AsyncSession, kb_id: str, *, tenant_id: str) -> int:
    """该租户在该知识库下的文档总数（静态配额「单库文档数」的唯一计数处，票 #79）。"""

    result = await db.execute(_count_by_kb_statement(kb_id, tenant_id=tenant_id))
    return int(result.scalar_one())


async def participating_snapshot(
    db: AsyncSession, kb_id: str, *, tenant_id: str
) -> tuple[int, int | None]:
    """起构建那一刻的**参与篇数与统一稿号**：``(count, max(current_version))``。

    这是 #214 那两列快照的唯一来源，两条聚合读共用同一份过滤条件（``status=1``：只有成功
    的文档有可切的正文；失败 / 排队中的文档该由单篇重试解决，不属于构建）。

    **为什么是两条语句而不是把文档行读一遍**：``start_build`` 跑在请求路径上（控制台改设置
    就会发起），一次把整库文档连同正文读进内存，等于让一次设置保存去背起整库的正文体积。
    两条 ``SELECT count / max`` 只读索引与指针；而「入口不遍历文档行」也正是 ADR-0034
    决策三写下的形状（按篇投递之后更是如此）。

    **为什么稿号取最大值**：库里的文档各自演进（回滚会把某篇的指针往回切），而一次构建产出
    的切片必须统一算一稿——混着数的后果是同一批切片一半旧稿一半新稿（ADR-0031 决策七）。
    取最大值是「这一库的稿号已经走到第几」唯一不需要猜的口径；真值判据按它数，缺一篇就是
    缺一篇，不会因为某篇往回滚过就把一次正常的构建判成完成。没有可参与的文档时回 ``None``
    （不是 0）：``None`` 的意思才是「这一稿没法挑」。
    """

    conditions = (
        Document.kb_id == kb_id,
        Document.tenant_id == tenant_id,
        Document.status == DocumentStatus.SUCCESS,
    )
    count_statement = select(func.count()).select_from(Document).where(*conditions)
    draft_statement = select(func.max(Document.current_version)).where(*conditions)
    count = int((await db.execute(count_statement)).scalar_one())
    draft = (await db.execute(draft_statement)).scalar_one()
    return count, (int(draft) if draft is not None else None)


# ──────────────── 写路径的行锁（第 12 步票 #175：建版本）


def lock_statement(document_id: str, *, tenant_id: str):
    """建版本前的**锁读**语句：``SELECT ... WHERE id + tenant FOR UPDATE``。

    ## 为什么必须有这一句

    稿号是 ``max(version) + 1``（列上没有默认值，见
    ``app.models.document_version``）。没有行锁时，两个提交会各自读到同一个 ``max``、
    算出同一个稿号，然后由 ``(document_id, version)`` 唯一约束把其中一个打回去——
    用户看到的是一次莫名其妙的「请重试」，而它本来就是一次可以正常串行化的写。

    锁的对象是**文档行**（不是版本表）：版本表的行还不存在，锁不了；而「这篇文档的
    稿次时间轴」正好就是文档行上的当前稿指针，锁它就等于锁住了这条时间轴。

    ## 为什么租户条件也在锁语句里

    跨租户一律「不存在」（ADR-0006）：锁不到行就等于「没有这篇文档」，随后的守卫照旧
    抛 20016。把租户过滤留在外面（先按 id 锁、再判租户）会让别人的请求等在这把锁上
    ——一次跨租户探测就变成了一次针对目标文档的锁等待。

    SQLite 方言会忽略 ``FOR UPDATE``（它没有行锁），所以离线用例只能断言**语句形状**；
    真正的串行化由 PostgreSQL 给。
    """

    return (
        select(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .with_for_update()
    )


async def lock_for_version(
    db: AsyncSession, document_id: str, *, tenant_id: str
) -> Document | None:
    """按 id + 租户锁住文档行并读回它；没有这一行就 ``None``（=「文档不存在」）。

    调用方在拿到这把锁之后才允许算稿号、写版本行、切指针、置处理中——顺序是契约的一部分：
    先锁再读，读到的 ``max`` 与 ``current_version`` 才是别人的提交已经落地的那个值。
    ``scalars().first()`` 而不是 ``scalar_one_or_none()``：替身会话对锁读只实现前者
    （与 ``documents.get_by_id_for_tenant_async`` 的收尾一致）。
    """

    return (await db.execute(lock_statement(document_id, tenant_id=tenant_id))).scalars().first()
