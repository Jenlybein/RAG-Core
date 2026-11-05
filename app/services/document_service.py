"""文档业务：同步索引闭环 + 状态机。

流程：校验知识库 → 建 Document(status=3) 并先提交 → 调 IndexingService →
回写 status=1。索引失败时回滚向量写入，把已落库的文档改成 status=2 并记
error_message，响应仍返回 code=0（业务失败不抛 5xx）。

索引链路失败仍属「部分成功」：调用方只拿到一句脱敏固定文案，真正的内部异常
原文连同文档 / 租户 / 知识库标识只写进 ERROR 日志，便于按标识回查。
应用日志只放 ID 与派生量，不放检索文本原文、文档内容或 API Key。

跨租户访问与知识库不存在都抛 KNOWLEDGE_BASE_NOT_FOUND，由 HTTP 入口适配器统一
翻译成 20008「不存在」——不返回「无权限」，避免泄露资源存在性。
"""

import structlog
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.models.document import Document
from app.repositories import document_repository, knowledge_base_repository
from app.services.indexing_service import IndexingService
from app.utils.id_generator import new_document_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)

# 索引链路失败时回传给调用方的唯一原因文案：不含异常对象、堆栈、路径或 SQL。
INDEXING_FAILED_MESSAGE = "document indexing failed, please retry or contact support"


async def upload(
    db: Session, *, tenant_id: str, kb_id: str, title: str, content: str
) -> dict:
    kb = knowledge_base_repository.get_by_id(db, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)

    doc = Document(
        id=new_document_id(),
        tenant_id=tenant_id,
        kb_id=kb_id,
        title=title,
        content=content,
        source_type="text",
        status=DocumentStatus.PROCESSING,
    )
    document_repository.create(db, doc)
    # 先落 PROCESSING 并提交：索引失败时这条记录仍然存在，才能回写成 FAILED。
    # 阶段 3 改异步后，前端也靠这个已存在的状态轮询。
    document_id = doc.id
    db.commit()

    try:
        chunk_count = await IndexingService().index(db, doc=doc, content=content)
        document_repository.update_status(db, doc, DocumentStatus.SUCCESS)
        db.commit()
    except Exception:
        logger.exception(
            "document indexing failed",
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
        )
        db.rollback()
        fresh = document_repository.get_by_id(db, document_id)
        if fresh is not None:
            document_repository.update_status(
                db, fresh, DocumentStatus.FAILED, INDEXING_FAILED_MESSAGE
            )
            db.commit()
        return {
            "document_id": document_id,
            "kb_id": kb_id,
            "status": DocumentStatus.FAILED,
            "chunk_count": 0,
            "error_message": INDEXING_FAILED_MESSAGE,
        }

    logger.info(
        "document indexed",
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        chunk_count=chunk_count,
    )
    return {
        "document_id": document_id,
        "kb_id": kb_id,
        "status": DocumentStatus.SUCCESS,
        "chunk_count": chunk_count,
        "error_message": None,
    }