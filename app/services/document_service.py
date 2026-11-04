"""文档业务：同步索引闭环 + 状态机。

流程：校验知识库 → 建 Document(status=3) 并先提交 → 调 IndexingService →
回写 status=1。索引失败时回滚向量写入，把已落库的文档改成 status=2 并记
error_message，响应仍返回 code=0（业务失败不抛 5xx）。

跨租户访问与库不存在都返回 None，由 API 层统一翻译成 20008「不存在」——
不返回「无权限」，避免泄露资源存在性。
"""

from sqlalchemy.orm import Session

from app.models.document import Document
from app.repositories import document_repository, knowledge_base_repository
from app.services.indexing_service import IndexingService
from app.utils.id_generator import new_document_id
from app.utils.status import DocumentStatus


async def upload(
    db: Session, *, tenant_id: str, kb_id: str, title: str, content: str
) -> dict | None:
    kb = knowledge_base_repository.get_by_id(db, kb_id)
    if kb is None or kb.tenant_id != tenant_id:
        return None

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
    except Exception as exc:
        db.rollback()
        fresh = document_repository.get_by_id(db, document_id)
        if fresh is not None:
            document_repository.update_status(db, fresh, DocumentStatus.FAILED, str(exc))
            db.commit()
        return {
            "document_id": document_id,
            "kb_id": kb_id,
            "status": DocumentStatus.FAILED,
            "chunk_count": 0,
            "error_message": str(exc),
        }

    return {
        "document_id": document_id,
        "kb_id": kb_id,
        "status": DocumentStatus.SUCCESS,
        "chunk_count": chunk_count,
        "error_message": None,
    }