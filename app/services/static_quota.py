"""静态资源配额：库数、单库文档数、并发处理数 —— 全都在**写路径**上到库里数行。

与 :mod:`app.services.quota_gate` 刻意分成两个模块：那两道关卡记的是「用掉了多少次」，
每秒、每天都会变，所以进 Redis；这三项问的是「现在有多少行」，答案本来就只有一个地方
——数据库。在 Redis 里再存一份计数，就得在删除文档、状态流转、Worker 崩溃、重建索引
每一条路径上同步它，漏一处就是两个真相，而错的那一个恰好会被用来拒绝客户。

## 为什么必须落在写路径

父 spec 的原话是「条件写路径也必须校验套餐：面板建库、上传文档走的是同一套静态配额
校验，否则控制台就是绕过套餐的后门」。这条要求翻译成调用点就是：

| 写路径 | 校验 | 数的是 |
| --- | --- | --- |
| 建知识库（``knowledge_base_service.create_kb``） | :func:`ensure_kb_quota` | 这个租户的库数 vs ``max_kb`` |
| 上传文档（``document_service.upload``） | :func:`ensure_upload_quota` | 这个库的文档数 vs ``max_documents_per_kb``；再数全租户处理中文档 vs ``max_processing_documents`` |
| 重建索引（``POST /documents/{id}/reindex``） | :func:`ensure_processing_quota` | 全租户处理中文档 vs ``max_processing_documents`` |

把限制写在「控制台接口」上而不是这三处，是同一件事的另一种失败方式：脚本、CLI、将来
另一个入口只要不经过那个接口，套餐就形同虚设。

## 并发处理数是三条里最要紧的一条

没有它，一个租户一次推一百份文档就能把后台 Worker 全占住，其他租户的文档只能排队——
**这是唯一一条「不超自己的上限也会伤害别人」的配额**，也是最容易漏写的一条：上传是一个
一个来的，单看一次请求都很合理。

判定口径是「status=处理中 **且**（还没被抢占 **或** 租约仍然有效）」（
``document_repository.count_processing_for_tenant``）：超出租约的行按 Worker 卡死处理，
不占名额——它们本来就不占任何 Worker，把它们也数进去等于一次崩溃永久吃掉一个名额。

超限一律 :attr:`~app.core.exceptions.ErrorCode.QUOTA_EXCEEDED`（``20014``）：与日配额
同一个码，因为调用方的反应是同一个——「提额或换套餐」，重试没有意义。它与秒级限流
（``20005``，该退避重试）刻意分开。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import ErrorCode
from app.db.dual_session import DBSession
from app.models.plan import PlanSpec
from app.repositories import document_repository, knowledge_base_repository
from app.services.quota_limits import ensure_under_limit


def ensure_kb_quota(db: Session, *, tenant_id: str, plan: PlanSpec) -> None:
    """建库之前：这个租户的库数还够不够（``plan.max_kb``）。"""

    used = knowledge_base_repository.count_by_tenant(db, tenant_id)
    _ensure_under(
        used=used,
        limit=plan.max_kb,
        what=f"knowledge base count of tenant {tenant_id}",
        plan=plan,
    )


def ensure_upload_quota(
    db: Session, *, tenant_id: str, kb_id: str, plan: PlanSpec
) -> None:
    """上传之前：这个库装得下（``max_documents_per_kb``），且租户还有并发名额。

    两条**都在这里**、不在调用方分别调：上传是唯一会同时撞上这两条上限的动作，把它们
    绑成一个函数之后，第二个上传入口很难只接一半（只接到单库文档数、漏掉并发数，
    正是本票说的「最容易忘的那一条」）。
    """

    used_documents = document_repository.count_by_kb(db, kb_id, tenant_id=tenant_id)
    _ensure_under(
        used=used_documents,
        limit=plan.max_documents_per_kb,
        what=f"document count of knowledge base {kb_id}",
        plan=plan,
    )
    ensure_processing_quota(db, tenant_id=tenant_id, plan=plan)


def ensure_processing_quota(db: Session, *, tenant_id: str, plan: PlanSpec) -> None:
    """再入队一份解析任务之前：这个租户还有没有并发处理名额。

    上传与重建都调它。重建尤其不能漏：它跳过「单库文档数」（文档本来就在库里），
    却照样往队列里塞一份工作。
    """

    used = document_repository.count_processing_for_tenant(db, tenant_id=tenant_id)
    _ensure_under(
        used=used,
        limit=plan.max_processing_documents,
        what=f"concurrently processing documents of tenant {tenant_id}",
        plan=plan,
    )


def _ensure_under(*, used: int, limit: int, what: str, plan: PlanSpec) -> None:
    """``used >= limit`` 就拒绝：额度 N 表示「最多 N 行」。

    判据与日配额共用 :func:`app.services.quota_limits.ensure_under_limit`（那边是
    「已经记了 N 次」）；这里只把套餐名缀在句尾，让运维一眼看出该找哪一档。
    """

    ensure_under_limit(
        used=used,
        limit=limit,
        code=ErrorCode.QUOTA_EXCEEDED,
        what=what,
        suffix=f"(plan {plan.code!r})",
    )


# ───────────────────────────────────── 异步孪生（第 11 步票 #132：上传登记链路）
#
# 判据、错误码（20014）与文案逐字相同，只是数行那两次查询走异步会话。``ensure_kb_quota``
# 没有孪生：建库在控制面，随票 #130 一起搬。


async def async_ensure_upload_quota(
    db: DBSession, *, tenant_id: str, kb_id: str, plan: PlanSpec
) -> None:
    """:func:`ensure_upload_quota` 的异步孪生：两条上限**都在这里**，不拆给调用方。"""

    used_documents = await document_repository.async_count_by_kb(
        db, kb_id, tenant_id=tenant_id
    )
    _ensure_under(
        used=used_documents,
        limit=plan.max_documents_per_kb,
        what=f"document count of knowledge base {kb_id}",
        plan=plan,
    )
    await async_ensure_processing_quota(db, tenant_id=tenant_id, plan=plan)


async def async_ensure_processing_quota(
    db: DBSession, *, tenant_id: str, plan: PlanSpec
) -> None:
    """:func:`ensure_processing_quota` 的异步孪生（上传与重建都调它）。"""

    used = await document_repository.async_count_processing_for_tenant(
        db, tenant_id=tenant_id
    )
    _ensure_under(
        used=used,
        limit=plan.max_processing_documents,
        what=f"concurrently processing documents of tenant {tenant_id}",
        plan=plan,
    )
