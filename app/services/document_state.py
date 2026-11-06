"""文档状态守卫：把「有效处理中」的判定收成一处，删除与重建共用。

有效处理中 = ``status=PROCESSING`` 且租约未过期（``index_claimed_at`` 落在
``[now - lease, now]`` 窗口内）。租约过期视为 Worker 卡死，允许被删除 / 重建，
与仓库层 CAS 抢占的条件保持一致；否则 Worker 崩一次，这份文档就永久卡在
「处理中」且永远不报错。

守卫一律写成独立函数，删除与重建共用同一个 ``is_effectively_processing``，
不要各自复制一份 ``if``。
"""

from app.core.exceptions import AppError, ErrorCode
from app.models.document import Document
from app.repositories.document_repository import DOCUMENT_LEASE_SECONDS, is_lease_alive
from app.utils.status import DocumentStatus


def is_effectively_processing(
    document: Document, *, lease_seconds: int = DOCUMENT_LEASE_SECONDS
) -> bool:
    """这份文档是否真的有人在处理（有效租约内的 PROCESSING）。"""

    return is_lease_alive(document, lease_seconds=lease_seconds)


def ensure_deletable(document: Document) -> None:
    """删除文档的前置守卫：有效处理中拒绝（20025）。"""

    if is_effectively_processing(document):
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)


def ensure_reindexable(document: Document, *, force: bool) -> None:
    """重建索引的前置守卫：有效处理中拒绝（20025）；成功但未 force 拒绝（20027）。

    失败文档直接允许重建；卡死（租约过期）也允许，与 CAS 的可抢占条件一致。
    """

    if is_effectively_processing(document):
        raise AppError(ErrorCode.DOCUMENT_IS_PROCESSING)
    if document.status == DocumentStatus.SUCCESS and not force:
        raise AppError(ErrorCode.REINDEX_REQUIRES_FORCE)
