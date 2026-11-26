"""静态资源配额：库数、单库文档数 —— 全都在**写路径**上到库里数行。

与 :mod:`app.services.quota_gate` 刻意分成两个模块：那两道关卡记的是「用掉了多少次」，
每秒、每天都会变，所以进 Redis；这两项问的是「现在有多少行」，答案本来就只有一个地方
——数据库。在 Redis 里再存一份计数，就得在删除文档、状态流转、Worker 崩溃、重建索引
每一条路径上同步它，漏一处就是两个真相，而错的那一个恰好会被用来拒绝客户。

## 为什么必须落在写路径

父 spec 的原话是「条件写路径也必须校验套餐：面板建库、上传文档走的是同一套静态配额
校验，否则控制台就是绕过套餐的后门」。这条要求翻译成调用点就是：

| 写路径 | 校验 | 数的是 |
| --- | --- | --- |
| 建知识库（``knowledge_base_service.create_kb``） | :func:`ensure_kb_quota` | 这个租户的库数 vs ``max_kb`` |
| 上传文档（``document_service.upload``） | :func:`ensure_upload_quota` | 这个库的文档数 vs ``max_documents_per_kb`` |

把限制写在「控制台接口」上而不是这两处，是同一件事的另一种失败方式：脚本、CLI、将来
另一个入口只要不经过那个接口，套餐就形同虚设。

## 只剩「容量型」两条（票 #218）

改造前这里还有第三条「并发处理数」：它数「索引中且租约有效」的行，在**所有**会入队解析
任务的写入口（上传、两条重建入口、建版本、回滚）上判。它已被删除，理由与代价写在
ADR-0034 决策八：那个「索引中」本身不可信（状态回写曾经静默失效，租约过期又代表没人跑），
而**入口拒绝从来不是对「别人排队变长」的处置**——处置那件事的是队列与调度。

于是这里只剩两条**容量**上限，它们只回答「这个租户自己装不装得下」，不控制并发、
也不保护别人（保护别人的手段是构建专用队列与公平调度，后者明确没做）。写入口因此
**一律接受**，上传与重建都只是往队列里排一件事。

## 每一条上限有自己的码与文案（票 #219）

改造前这几条共用 :attr:`~app.core.exceptions.ErrorCode.QUOTA_EXCEEDED`（``20014``）与
一句「等额度重置，或去密钥配置里调整上限」。那句话对库数满、文档数满、并发名额满**三种
情形都是错的**：它们要的是「删点东西就能继续」，不是等重置；要动的是**套餐**（运营改），
不是密钥配置（用户自己改）。错误码相同则前端连分辨都做不到。

现在按「调用方该做什么」分码（ADR-0035 决策七）：

* 库数满 → ``20034``，处置是**删库**或提额；
* 单库文档数满 → ``20035``，处置是**删该库里的旧文档**或提额。

码、机器可读的 ``reason``、以及那句中文人话都收在 :class:`CapacityQuota` 表里
（:data:`KB_COUNT_QUOTA` / :data:`KB_DOCUMENT_QUOTA`）——三样必须一起变，否则就会出现
「20034 配着『等额度重置』」这种半截改造。响应 ``data`` 里同时带 ``used`` / ``limit`` /
``plan``，面板不必去解析文案里的数字。

## 只剩异步面

（第 11 步票 #130）它们数行的调用点全部在事件循环上，迁移期那对同步孪生已经删掉——
判定与文案本来就只有一份，现在连会话也只有一种。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ErrorCode
from app.models.plan import PlanSpec
from app.repositories import document_repository, knowledge_base_repository
from app.services.quota_limits import ensure_under_limit


@dataclass(frozen=True)
class CapacityQuota:
    """一种**容量型**上限的对外口径：专属错误码 + 机器可读原因 + 中文人话模板。

    三样东西收在同一个对象里，是因为它们必须一起变：

    * 只换码不换文案 → 用户看到「20034 + 等额度重置」，还是那句错话；
    * 只换文案不换码 → 前端仍按旧码判「库满了」，而它已经分辨不出来了。

    ``message`` 是模板而不是成品：已用 / 上限 / 套餐三个数字要到判定那一刻才知道。
    模板里只许出现 ``{used}`` / ``{limit}`` / ``{plan}`` 三个占位。
    """

    code: ErrorCode
    reason: str
    message: str


#: 「库数超套餐」：免费档 1 个库，建第 2 个就撞它。
KB_COUNT_QUOTA: CapacityQuota = CapacityQuota(
    code=ErrorCode.KB_COUNT_QUOTA_EXCEEDED,
    reason="kb_count",
    message=(
        "知识库数量已达套餐上限：已用 {used} 个，上限 {limit} 个，当前套餐 {plan}；"
        "删掉不用的知识库就能继续建，或找运营把套餐上限提上去"
    ),
)

#: 「单库文档数超套餐」：删的是**这个库**里的旧文档，不是别的库的。
KB_DOCUMENT_QUOTA: CapacityQuota = CapacityQuota(
    code=ErrorCode.KB_DOCUMENT_QUOTA_EXCEEDED,
    reason="kb_document_count",
    message=(
        "这个知识库的文档数已达套餐上限：已用 {used} 篇，上限 {limit} 篇，当前套餐 {plan}；"
        "删掉该库里的旧文档就能继续上传，或找运营把套餐上限提上去"
    ),
)


async def ensure_kb_quota(db: AsyncSession, *, tenant_id: str, plan: PlanSpec) -> None:
    """建库之前：这个租户的库数还够不够（``plan.max_kb``）。"""

    used = await knowledge_base_repository.count_by_tenant(db, tenant_id)
    _ensure_under(quota=KB_COUNT_QUOTA, used=used, limit=plan.max_kb, plan=plan)


async def ensure_upload_quota(
    db: AsyncSession, *, tenant_id: str, kb_id: str, plan: PlanSpec
) -> None:
    """上传之前：这个库还装得下（``max_documents_per_kb``）。

    **只有这一条**：上传不再受「同时处理中篇数」限制（#218，ADR-0034 决策八）——处理中
    的东西进了队列就排着，拒掉第 N+1 篇既不会让队列变短，也不是对「别人排队变长」的
    处置。函数留在这里（而不是直接写在调用方里），是为了让「上传要过哪几条上限」仍然
    只有一处答案：改这条口径时不必去找第二个上传入口。
    """

    used_documents = await document_repository.count_by_kb(db, kb_id, tenant_id=tenant_id)
    _ensure_under(
        quota=KB_DOCUMENT_QUOTA,
        used=used_documents,
        limit=plan.max_documents_per_kb,
        plan=plan,
    )


def _ensure_under(*, quota: CapacityQuota, used: int, limit: int, plan: PlanSpec) -> None:
    """``used >= limit`` 就按**这一种**配额的口径拒绝：码、文案、data 都取自 ``quota``。

    判据不在这里，仍然只有 :func:`app.services.quota_limits.ensure_under_limit` 那一处
    （``used >= limit``）；这里只负责把「哪一种配额」翻译成用户看得懂的那三样东西。
    所以加一种容量上限 = 往表里加一行 :class:`CapacityQuota`，不是在函数里多一个分支。
    """

    ensure_under_limit(
        used=used,
        limit=limit,
        code=quota.code,
        message=quota.message.format(used=used, limit=limit, plan=f"{plan.name}（{plan.code}）"),
        data={"reason": quota.reason, "used": used, "limit": limit, "plan": plan.code},
    )
