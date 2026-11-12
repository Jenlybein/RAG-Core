"""归属索引的数据访问：唯一能碰 ``retrieval_traces`` 表的一层。

**检索链路只写不读**。写侧只 ``flush`` 不 ``commit``（事务由 Service 收口，与其它仓储一致）。

读侧三处，而且**必须带租户**：控制台按 ``log_id`` 读回瀑布之前，先要证明「这一行
属于当前租户」（#103）；反馈提交前的归属校验也走它（#105）——那条路径要判的三件事
（存在 / ``trace_id`` 对得上 / 是当前租户的）本来就要在同一行上读完，分成两次查只会让
「跨租户」多一次泄露的机会。回读**不进检索链路**：链路一旦开始读这张表，旁路就变回主链
路的依赖，而这张表的意义正是「链路不需要它也能跑完」。

**控制面的读已经换到 ``AsyncSession``**（票 #129）：反查、最近列表与瀑布的归属校验都是
``async``，且没有同步孪生——它们的调用点全在这一票里，搬一次就搬完。唯一还留同步孪生的
是 :func:`get_by_id_for_tenant`：反馈提交那条**写**路径（#128）仍在同步会话上用它，
:func:`get_by_id_for_tenant_async` 是瀑布读的那一份。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.retrieval_trace import RetrievalTrace


def create(db: Session, trace: RetrievalTrace) -> RetrievalTrace:
    db.add(trace)
    db.flush()
    return trace


def get_by_id_for_tenant(db: Session, log_id: str, *, tenant_id: str) -> RetrievalTrace | None:
    """按 ``id``（对外即 ``log_id``）**且** ``tenant_id`` 取一行；缺失与跨租户都返回 ``None``。

    租户是**强制参数、不是可选过滤器**：调用方拿到 ``None`` 一律按「不存在」处理，
    可区分的拒绝会泄露「这个 log_id 在别人那儿存在」。这条口径与
    ``document_repository.get_by_id_for_tenant`` 逐字同构，是 ADR-0006 的隔离精神在控制面
    这一侧的落地；无租户的按 id 读取本模块**不提供**（那张表的读者只有控制台与反馈）。

    实现上先按主键取、再在**这里**判租户，而不是把条件拼进一条 ``select``：主键查询走
    会话的 identity map，少一次 SQL 往返；而「不属于你就当不存在」是**这一层**的结论，
    拼不拼进 SQL 都得出同一个答案——放在一处，跨租户的口径就只有一份。真库与离线替身
    （``RecordingSession.get``）走的也是同一条路径。
    """

    row = db.get(RetrievalTrace, log_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


async def get_by_id_for_tenant_async(
    db: AsyncSession, log_id: str, *, tenant_id: str
) -> RetrievalTrace | None:
    """:func:`get_by_id_for_tenant` 的异步孪生（控制面瀑布，票 #129）。

    判定写在**一处**：租户比对仍在 Python 这一侧，只把「按主键取」换成 ``await db.get``
    ——同一口径两种会话，跨租户的答复不会因为换了栈而变。
    """

    row = await db.get(RetrievalTrace, log_id)
    if row is None or row.tenant_id != tenant_id:
        return None
    return row


async def get_by_trace_id_for_tenant(
    db: AsyncSession, trace_id: str, *, tenant_id: str
) -> RetrievalTrace | None:
    """按 ``trace_id`` 且 ``tenant_id`` 取一行（#106 的反查，票 #129 起异步）。

    **为什么需要反查**：检索响应里只有 ``metadata.trace_id``（ADR-0010 第五次修订的红线：
    除该字段外逐字节不变），反馈与瀑布要的却是 ``log_id``；而 ``trace_id`` 是 ``log_id``
    的单向派生（sha256 前 32 位），**推不回去**。于是在控制面开一个只读反查，把「这根线」
    翻回「那一行」，而不是往数据面响应里加字段。

    租户同样是强制参数：跨租户与不存在一律 ``None``，与 ``get_by_id_for_tenant`` 同一口径。
    """

    statement = select(RetrievalTrace).where(
        RetrievalTrace.trace_id == trace_id, RetrievalTrace.tenant_id == tenant_id
    )
    return (await db.execute(statement)).scalar_one_or_none()


async def list_recent_for_tenant(
    db: AsyncSession, *, tenant_id: str, limit: int = 20
) -> list[RetrievalTrace]:
    """按时间倒序取本租户最近若干条归属索引（控制台「最近检索」列表）。

    **这是本模块唯一的「列表」读法，也是唯一一处不按 id 点查的地方**——所以它必须自带
    ``limit``：没有上限的列表查询在表长大之后会变成一次全表扫，而控制台只是让人「看最近
    几次」。上限由路由层的 query 参数给，默认 20、最大 100。

    只返回结构字段（`id` / `trace_id` / 时间 / 耗时 / 结局），**没有任何原文**：这张表本来
    就没有原文可返回（ADR-0021 决策 1），列表页因此不需要额外脱敏。
    """

    statement = (
        select(RetrievalTrace)
        .where(RetrievalTrace.tenant_id == tenant_id)
        .order_by(RetrievalTrace.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(statement)).scalars().all())
