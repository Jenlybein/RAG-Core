"""文档状态守卫：把「真的有人在跑」的判定收成一处，删除 / 重建 / 建版本 / 回滚共用。

有效处理中 = ``status=PROCESSING`` 且租约未过期（``index_claimed_at`` 落在
``[now - lease, now]`` 窗口内）。这是「此刻有 Worker 拿着这一篇」的**唯一**判据
（ADR-0034 决策一），与仓库层 CAS 抢占的条件一致。

## 排队中为什么允许删除 / 重新提交

``QUEUED``（4）的含义是「已经收下、还没被任何 Worker 接走」——它没有租约，也没有任何
东西正在被写。在这时删掉或换一稿，撤回的只是一条**还没被执行的意图**：迟到的任务在
抢占那一步会碰上 ``rowcount=0``（那一行已经不在了，或者状态已经被改掉），于是不产生
任何切片。把它也拒掉，等于让「排队」变成囚笼：用户传错一个文件，要等它跑完才能撤回，
而排队可能来自一次整库重建，前后十几分钟。

**只有租约有效才回 20025**：那个码说的是「此刻有人在写这篇的切片，现在切会变成掷骰子」。
排队中没有人在写，拿同一个码去拒，理由就不成立了——用户会以为系统在忙，其实是自己在等。

租约过期同样不算在跑：Worker 崩一次不能让这份文档永久卡在「处理中」且永远不报错
（这正是三态时代的那个坑）。

守卫一律写成独立函数，删除 / 重建 / 建版本 / 回滚共用同一个
``is_effectively_processing``，不要各自复制一份 ``if``。
"""

from app.core.exceptions import AppError, ErrorCode
from app.models.document import Document
from app.repositories.document_repository import DOCUMENT_LEASE_SECONDS, is_lease_alive
from app.utils.status import DocumentStatus


def is_effectively_processing(
    document: Document, *, lease_seconds: int = DOCUMENT_LEASE_SECONDS
) -> bool:
    """这份文档此刻是否真的有人在处理（``PROCESSING`` 且租约有效）。

    排队中（4）恒为 ``False``：它没有租约，也就没有人在写它的切片。
    """

    return is_lease_alive(document, lease_seconds=lease_seconds)


def ensure_deletable(document: Document) -> None:
    """删除文档的前置守卫：有效处理中拒绝（20025）；排队中可以直接删。

    排队中删除 = 撤回排队（ADR-0034 决策一）：「排队中的两个新权能」之一。
    """

    _ensure_not_processing(document)


def ensure_versionable(document: Document) -> None:
    """建版本 / 回滚的前置守卫：有效处理中拒绝（20025，复用既有的「文档处理中」）。

    为什么复用 20025 而不新造一个「忙」的码：**一个含义一个码**。再造一个，调用方就得
    靠「两个码都要处理」才不误判，漏掉一个的症状是「偶发一个没见过的码」。租约口径也与
    删除 / 重建完全一致：排队中（4）没有租约，重新提交它就是「替换排队」；``PROCESSING``
    但 ``index_claimed_at`` 为空（迁移期兼容形状）或租约过期（Worker 卡死）都不算
    「有人在处理」——刚提交过一稿的文档正处在排队态，那时再来一稿是正常的。
    """

    _ensure_not_processing(document)


def _ensure_not_processing(document: Document) -> None:
    """四条写路径共用的那一句：有效处理中就拒绝。"""

    if is_effectively_processing(document):
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)


def ensure_reindexable(document: Document, *, force: bool) -> None:
    """重建索引的前置守卫：有效处理中拒绝（20025）；成功但未 force 拒绝（20027）。

    排队中直接允许重建（=重新提交，替换排队，不允许两个任务同时跑：迟到的那个抢不到）。
    失败文档直接允许重建；卡死（租约过期）也允许，与 CAS 的可抢占条件一致。
    """

    if is_effectively_processing(document):
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)
    if document.status == DocumentStatus.SUCCESS and not force:
        raise AppError(ErrorCode.REINDEX_REQUIRES_FORCE)
