"""归属索引的写入：**独立短事务**，失败只留 warning（#101）。

三条约束都是从红线直接推出来的，改这里之前先看 ADR-0021 决策 2 与 ADR-0010 第五次修订：

1. **不能用请求会话**。``get_async_db`` 产出的事务语义是只读的，而且从不 ``commit``——
   在它上面顺手 ``INSERT`` 会被 ``close()`` 丢掉（看起来「写过了」，其实什么都没留下）。
   所以这里自己开一个会话、自己提交、自己关：这是一次**独立短事务**，与请求事务无关。
2. **会话工厂只能有一个入口**。拿不到 ``Depends`` 的路径都走 :func:`app.db.session_factory
   .async_session_factory`（中间件、Celery、Provider 装配走的是它的同步孪生），测试也就
   只需要换掉这一处，不必让整个离线套件真的去连 PostgreSQL。**不要在业务函数里写
   ``if testing:``**：那种分支一旦存在，生产路径与测试路径就是两条代码。
3. **这里永远不抛**。写入是旁路：开不出会话、写不进去、``commit`` 失败、连 ``close`` 失败，
   都只记一条 ``warning``。响应里的 ``trace_id`` 在调用本函数**之前**就已经派生好了，所以
   「写库失败」不会让那个字段缺席——反过来「写成功才拿到 id」就等于把契约挂在旁路的健康度上。

**第 11 步票 #127 起这一条写入也是异步的**：检索链路整体在异步栈上，而它曾经的同步
``session_factory()`` + 同步 ``commit_now`` 会在事件循环里做一次阻塞往返（压测里那 7 次
「主循环同步 DB 调用」中有一次就是它）。现在它走异步会话工厂与 :func:`app.db.transaction
.acommit_now`——**提交点仍是那一个文件**，只是换成了 ``await``。

日志里只放 id 与错误**类型**：异常文本可能夹带连接串，问句原文更是一个字都不许进日志
（见 ``tests/test_retrieval_log_hygiene.py``）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import now
from app.db.session_factory import async_session_factory, close_quietly
from app.db.transaction import async_transaction
from app.models.document import Document
from app.models.usage_event import UsageEvent
from app.repositories import api_key_repository, document_repository, retrieval_trace_repository
from app.services.trace_identity import delivered_doc_versions

logger = structlog.get_logger(__name__)

#: 写入失败时的固定消息。测试按它筛记录，运维按它告警——所以它是一句**固定**的话，
#: 不拼异常文本、不随调用点漂移。
TRACE_WRITE_FAILED_MESSAGE = "retrieval trace not recorded"


async def record_retrieval_trace(
    *,
    log_id: str,
    trace_id: str,
    tenant_id: str,
    kb_ids: list[str],
    index_generations: dict[str, int],
    doc_versions: dict[str, int],
    chunk_ids: list[str],
    missing_generation_documents: int,
    top_k: int,
    latency_ms: int,
    degraded_reason: str | None,
    empty_reason: str | None,
    endpoint: str | None = None,
    code: int | None = None,
    key_id: str | None = None,
    meter_r: int = 0,
    meter_rr: int = 0,
    meter_llm: int = 0,
) -> None:
    """把这一次请求写进 ``usage_events``（账 + 归属索引）；任何失败都降级成一条 warning。

    参数全是**已经算好的事实**（身份、范围、结局），这里不做判定也不改口径：
    ``degraded_reason`` / ``empty_reason`` 直接来自本次响应的 ``metadata.retrieval``，
    于是「库里那一行」与「回给调用方的那个响应」不会各说一套。

    ``index_generations`` 是「这次**每篇**落在第几代」（ADR-0025 第 2 次修订第 5 条）：
    ``{document_id: 代数}``，值来自交付单元上的 ``document_id`` → 该篇的**当前代**
    （:func:`delivered_generations`，一次按主键取 ``documents``）。**不从切片上读代**：
    关键词路的行里没有这一维，两路形状必须一致；也不从库级版本换算——版本轴已经退场。
    没有默认值：这个参数一旦可以省略，「某条路径忘了记代」就会变成一列安静的 ``{}``。
    只记**交付过的篇**，与 ``doc_versions`` / ``chunk_ids`` 同一批交付物（三列严格同源）。

    ``doc_versions`` 是「这次命中的是哪几稿」（#179）：与交付单元**同一份映射**
    （``document_id → doc_version``），由调用方从交付单元上那个真实列收口
    （:func:`app.services.trace_identity.delivered_doc_versions`）。**没有默认值**：
    这个参数一旦可以省略，「某条路径忘了记稿号」就会变成一列安静的 NULL，而这一列
    存在的全部意义正是「事后还答得出那次查的是第几稿」。多条路径都要显式给值。

    ``missing_generation_documents`` 是本次范围内「还没有任何产物」的篇数
    （``documents.current_index_generation IS NULL``，检索时整篇被跳过）。它由
    :func:`missing_generation_count` 决定：**只在结果不满额时**才有值，满额时恒 0。
    没有默认值，同样是为了让每条路径显式说出「这次我没有交付、也没有数缺代」。

    ``endpoint`` / ``code`` / ``key_id`` / 三本账是第 14 步（#200）补的六个字段：它们让这一行
    同时是**账**。新的调用点必须全部显式给——尤其是 ``code``：写成默认的 ``None`` 等于
    「这条不是一次调用」，看板会把它归进历史汇总。

    ``key_id`` 非空时顺手把 ``api_keys.last_used_at`` 推到这一刻：那一列从建表起就没人写过，
    而记账点恰好知道「这把 Key 刚刚用了一次」。
    """

    session: AsyncSession | None = None
    try:
        session = async_session_factory()()
        # 一笔独立短事务：入口仍是 ``app.db.transaction``（异步孪生），不是裸提交。
        async with async_transaction(session):
            await retrieval_trace_repository.create(
                session,
                UsageEvent(
                    id=log_id,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    key_id=key_id,
                    endpoint=endpoint,
                    code=code,
                    meter_r=meter_r,
                    meter_rr=meter_rr,
                    meter_llm=meter_llm,
                    kb_ids=list(kb_ids),
                    index_generations=dict(index_generations),
                    doc_versions=dict(doc_versions),
                    chunk_ids=list(chunk_ids),
                    missing_generation_documents=missing_generation_documents,
                    top_k=top_k,
                    latency_ms=latency_ms,
                    degraded_reason=degraded_reason,
                    empty_reason=empty_reason,
                ),
            )
            if key_id is not None:
                await api_key_repository.touch_last_used(session, key_id, now())
    except Exception as exc:  # noqa: BLE001 - 旁路：任何失败都不许冒到检索返回值上
        logger.warning(
            TRACE_WRITE_FAILED_MESSAGE,
            tenant_id=tenant_id,
            trace_id=trace_id,
            error=type(exc).__name__,
        )
    finally:
        await close_quietly(session, on_failure=_report_close_failure)


async def delivered_generations(
    db: AsyncSession,
    units: Iterable[Mapping[str, object]],
    *,
    tenant_id: str,
) -> dict[str, int]:
    """这次交付的每篇文档落在**第几代**：``{document_id: current_index_generation}``。

    为什么读的是 ``documents.current_index_generation``（一次按主键取库），而不是切片上的
    ``chunks.generation``：关键词路的行里**没有**这一维（ES 只镜像当前代，不带代数），
    两路形状必须一致，所以只能从「这一篇现在指着哪一代」这个库级指针回答。指针就是
    检索那次用到的可见性判据的另一半（``chunks.generation = documents.current_index_generation``），
    读它不会与「实际取数的那一代」漂开。

    ``NULL`` 的篇**不写进映射**：那是「这篇还没有任何产物」，检索时整篇被跳过——它本来就
    一片都没交付，给它编一个代数正是审计账本要抓的静默错归因。于是这份映射是
    :func:`app.services.trace_identity.delivered_doc_versions` 的**子集**（那一边同样只收
    能读出售价值的单元），三列同源但允许这一列更短。

    ``tenant_id`` 是强制过滤（ADR-0006）：按 id 读文档的数据面路径必须带租户，跨租户的
    id 猜测不该在这里读到别人的指针。代价是一次主键 + 租户的读，由每个交付路径各付一次。
    """

    # 文档 id 集合直接取自稿号那一份映射：两条列必须严格同源，谁也不许多出一篇。
    document_ids = sorted(delivered_doc_versions(units))
    if not document_ids:
        return {}

    statement = select(Document.id, Document.current_index_generation).where(
        Document.id.in_(document_ids),
        Document.tenant_id == tenant_id,
        Document.current_index_generation.is_not(None),
    )
    generations: dict[str, int] = {}
    for row in (await db.execute(statement)).all():
        document_id = getattr(row, "id", None)
        generation = getattr(row, "current_index_generation", None)
        if isinstance(document_id, str) and isinstance(generation, int):
            generations[document_id] = generation
    return generations


async def missing_generation_count(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_ids: Sequence[str],
    delivered_count: int,
    top_k: int,
    empty_reason: str | None,
) -> int:
    """本次范围内「还没有任何产物」的篇数；**只在结果不足时**才去数。

    为什么只在结果不足时数：满额（``delivered_count >= top_k`` 且没有 ``empty_reason``）
    说明缺代没有伤害这次调用——为每一个成功请求多打一次 ``documents`` 是没换来东西的固定
    成本。而结果少了时，这个数正好回答「是不是有几篇还没有产物，所以凑不满」，
    它是这次调用的结局的一部分，不是常驻统计。

    ``empty_reason`` 参与判据看起来与「不足」重复（空结果必然不满足额），但它来自
    **响应里那个字段**：两条判据任一成立都要数，宁可多打一次库，也不要因为某个路径把
    ``top_k`` 传成 0 之类的边角而漏数——那个数正是这次调用要解释的东西。

    计数 > 0 时留一条 warning（只记数字与库数，**不记问句原文**，见日志纪律）。
    """

    if empty_reason is None and delivered_count >= top_k:
        return 0

    missing = await document_repository.count_missing_generation_documents(
        db, tenant_id=tenant_id, kb_ids=kb_ids
    )
    if missing > 0:
        logger.warning(
            "documents without any index generation were skipped",
            kb_count=len(kb_ids),
            missing_generation_documents=missing,
        )
    return missing


def _report_close_failure(error_type: str) -> None:
    """把「关会话失败」翻译成一条 warning（消息与写库失败共用同一条固定文本）。"""

    logger.warning(TRACE_WRITE_FAILED_MESSAGE, error=error_type)
