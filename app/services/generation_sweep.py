"""代落后扫描（第 17 步现场九·第七步）：把「产物不是最新一代」的篇重新投一遍。

## 它补的是哪个洞

代下放到篇级之后，「这篇的产物是不是最新的」变成两个具体的问题：这一篇记下的切块参数
指纹是否等于**库当前设置**的指纹，以及它到底有没有产物（`current_index_generation` 为
`NULL`）。界面已经能按篇说出「N 篇待更新」（`count_stale_generations`），但**只有人能看见**
——没人点「重建」的时候，一个改过设置的库会永远停在旧切块上。

## 为什么不做成「改设置就自动重建」

改设置是控制面的一个请求，而重建是重活。在那个请求里顺手投 N 篇任务，会让一次设置保存
变成一次不可控的批量提交（一次点错就是整库重算，而且没有回手的余地）。放在**夜间**清扫
里则是：一次最多 `limit` 篇、每次跑完再说，反悔的窗口留到下一次运行。

## 判据比「N 篇待更新」多一条

`count_stale_generations` 只比指纹（那是给界面看的数）；这里还数
`current_index_generation IS NULL` 的篇——它们**一片产物都没有**，检索时被跳过。只按指纹
算会把它们漏掉：一篇从来没建成的 `SUCCESS` 文档，指纹可能恰好等于库当前设置（例如迁移
回填时按生效版本参数算的指纹），于是它永远不在「待更新」里，也永远不出现在检索结果里。

## 代价

* 每晚最多 `limit` 篇；库里真有一千篇待更新时要跑二十晚。这是有意的：宁可慢，也不要一夜
  之间把全库重算一遍、把队列堵住——检索在重建期间照旧服务当前代（这是代模型的好处）。
* 重投走的还是单篇重建那条路（`prepare_for_indexing` 置「排队中」→ `enqueue_document`），
  所以它与人工点重建**完全同一条规则**，不存在两套口径。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase, build_params_of
from app.repositories import document_repository
from app.services.index_generation import settings_fingerprint
from app.services.index_dispatch import enqueue_document
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

#: 夜间一次最多重投多少篇（理由见模块文档的「代价」）。
MAX_STALE_REDISPATCH_PER_NIGHT = 50


def _stale_predicate(fingerprint: str | None):
    """「这一篇的产物不是最新一代」的判据（库级指纹 + 有没有产物）。"""

    stale = (Document.settings_hash.is_(None)) | (Document.current_index_generation.is_(None))
    if fingerprint is not None:
        stale = stale | (Document.settings_hash != fingerprint)
    return stale


async def redispatch_stale_documents(
    db: AsyncSession, *, limit: int = MAX_STALE_REDISPATCH_PER_NIGHT
) -> int:
    """把代落后的篇重新排队，返回这一轮**真的投出去**的篇数。

    只在**同一笔事务**里置「排队中」：调用方（清扫器）负责提交。不提交的话
    「置为排队中 + 计数加一」会随会话关闭回滚，而消息已经发出去了——症状是 Worker 抢不到
    （它按状态 CAS），清扫器日志说有动作、库里一行没变，与「清扫器没跑」一模一样。
    """

    if limit <= 0:
        return 0

    kbs = (await db.execute(select(KnowledgeBase))).scalars().all()
    redispatched = 0

    for kb in kbs:
        remaining = limit - redispatched
        if remaining <= 0:
            break
        # 指纹在 Python 里算：它要读库行的设置并过一道规范键集（`build_params_of`），
        # 写成 SQL 等于把那份规则抄第二遍。
        fingerprint = settings_fingerprint(build_params_of(kb))
        rows = (
            await db.execute(
                select(Document.id, Document.tenant_id)
                .where(
                    Document.kb_id == str(kb.id),
                    Document.tenant_id == str(kb.tenant_id),
                    Document.status == DocumentStatus.SUCCESS,
                    _stale_predicate(fingerprint),
                )
                .order_by(Document.updated_at)
                .limit(remaining)
            )
        ).all()

        for document_id, tenant_id in rows:
            tenant = str(tenant_id)
            # `allow_success=True`：这里要的正是「已经是成功、但产物过时」那一批。
            prepared = await document_repository.prepare_for_indexing_async(
                db, str(document_id), tenant_id=tenant, allow_success=True
            )
            if not prepared:
                # 刚被别处（人工重建、另一个清扫器）抢先置成排队/索引中：跳过，不改口径。
                continue
            enqueue_document(
                document_id=str(document_id),
                tenant_id=tenant,
                kb_id=str(kb.id),
                request_id=None,
            )
            redispatched += 1
            logger.info(
                "sweeper requeued a document whose generation is behind",
                document_id=str(document_id),
                tenant_id=tenant,
                kb_id=str(kb.id),
            )

    return redispatched
