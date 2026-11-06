"""文档业务：异步上传登记 + 状态机。

上传只做登记：校验知识库 → 建 ``Document(status=PROCESSING)`` → 文件模式落盘 /
JSON 模式直接落正文 → ``commit`` → 投递索引任务 → 立即返回。**请求线程不解析、
不向量化**，进程内的耗时与文档大小无关；进度由 ``GET /documents?kb_id=`` 轮询，
后端的解析 / 切块 / 向量化与失败留痕、脱敏文案统一由 T4 的 Worker 任务负责。

落盘路径只由 ``document_storage`` 按服务端规则拼接，原始文件名只进
``source_filename`` 列，绝不进路径。

应用日志只放 ID 与派生量，不放检索文本原文、文档内容或 API Key。

跨租户访问与知识库不存在都抛 ``KNOWLEDGE_BASE_NOT_FOUND``，由 HTTP 入口适配器统一
翻译成 20008「不存在」——不返回「无权限」，避免泄露资源存在性。
"""

from pathlib import Path

import structlog
from sqlalchemy.orm import Session

from app.models.document import Document
from app.repositories import document_repository
from app.services.document_storage import SOURCE_TYPE_BY_EXTENSION, store_document_file
from app.services.index_dispatch import enqueue_document
from app.services.knowledge_base_access import ensure_visible
from app.utils.id_generator import new_document_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)


async def upload(
    db: Session,
    *,
    tenant_id: str,
    kb_id: str,
    title: str | None = None,
    content: str = "",
    filename: str | None = None,
    data: bytes = b"",
    request_id: str | None = None,
) -> dict:
    """登记一份文档并投递索引任务，返回可直接回给调用方的摘要。

    两种模式由入口按 content-type 决定：``filename`` 非空 = 文件模式（正文留空，
    Worker 从落盘文件解析）；否则 JSON 模式（``content`` 直接落库）。
    """

    ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)

    document_id = new_document_id()

    if filename is not None:
        source_file_path, source_filename = store_document_file(
            tenant_id=tenant_id, document_id=document_id, filename=filename, data=data
        )
        source_type = SOURCE_TYPE_BY_EXTENSION[Path(filename).suffix.lower()]
        doc = Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=title or Path(filename).stem,
            content="",
            source_type=source_type,
            source_file_path=source_file_path,
            source_filename=source_filename,
            status=DocumentStatus.PROCESSING,
        )
    else:
        doc = Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=title or "",
            content=content,
            source_type="text",
            status=DocumentStatus.PROCESSING,
        )

    document_repository.create(db, doc)
    # 先落 PROCESSING 并提交：状态查询在请求返回后立刻能看见这份文档；Worker
    # 崩溃时这张行也还在，留待租约过期后的重试 / 重建。
    db.commit()

    enqueue_document(
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        request_id=request_id,
        reparse=False,
    )

    logger.info(
        "documents accepted",
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        source_type=doc.source_type,
    )
    return {
        "document_id": document_id,
        "kb_id": kb_id,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "error_message": None,
    }
