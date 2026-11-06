"""索引流水线（Worker 侧）：解析 → 切块 → 向量化 → 先删后写 → 状态回写。

上传请求线程只登记并投递任务，绝不进来这里；进程内的耗时（尤其是 embedding 的
网络往返）完全由 Celery Worker 承担。

**两段事务**是这里最重要的不变量：
1. ``try_claim_for_indexing`` 抢到索引权后立刻 ``commit``。若把整条流水线包在
   行锁里，``GET /documents`` 在 Worker 跑完之前都会卡在这份文档那一行上。
2. 解析 / 切块 / 向量化之后，purge（先删旧切片）+ 写入新切片 + ``status=SUCCESS``
   在同一段事务里提交：调用方永远不会看到「旧切片没了、新切片还没写」或
   「切片写好了、状态还是处理中」的中间态。异常时由调用方（Celery 任务）回滚。

并发去重靠数据库 CAS，不靠 Celery 的 ``task_id``：``acks_late`` 让投递至少一次，
而 ``try_claim_for_indexing`` 让重复投递里的后来者抢不到，直接返回 0。这两者是
配套的，不要只留一半。

所有依赖（切块器 / embedding / 向量存储）都从构造参数注入，任务里绝不在方法体
内 ``new`` 具体类：测试靠注入替身，整份测试不需要网络、Broker 或数据库。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding import EmbeddingProvider, get_embedding_provider
from app.providers.vectorstores import VectorStore, get_vector_store
from app.repositories import document_repository
from app.services.document_ingestion import prepare_document_content
from app.utils.id_generator import new_chunk_id
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.status import DocumentStatus
from app.utils.text_splitter import TextSplitter

logger = structlog.get_logger(__name__)

# 索引链路失败时回传给调用方的唯一原因文案：不含异常对象、堆栈、路径或 SQL。
# 失败回写由 app.tasks.indexing._mark_failed 负责，它从这里 import；不要把异常
# 原文拼进来。
INDEXING_FAILED_MESSAGE = "document indexing failed, please retry or contact support"


async def purge_document_chunks(db: Session, document_id: str, stores: list[VectorStore]) -> None:
    """先删后写的第一半：遍历**存储列表**，逐个清掉该文档的旧切片。

    遍历的是列表而不是写死某一个存储，因此以后加 Elasticsearch 只需在默认接线里
    往 ``stores`` 追加一个元素（并让它实现 ``delete_by_document_id``），调用方一行
    都不用改。
    """

    for store in stores:
        await store.delete_by_document_id(db, document_id)


class IndexingService:
    """Worker 侧的索引编排：一份已登记的文档 → 可检索的切片。"""

    def __init__(
        self,
        *,
        splitter: TextSplitter | None = None,
        embeddings: EmbeddingProvider | None = None,
        stores: list[VectorStore] | None = None,
    ) -> None:
        if splitter is not None:
            self._splitter = splitter
        else:
            self._splitter = MarkdownStructuredSplitter(
                settings.CHUNK_SIZE,
                settings.CHUNK_OVERLAP,
                settings.TABLE_MAX_ROWS_PER_CHUNK,
            )
        self._embeddings = embeddings if embeddings is not None else get_embedding_provider()
        self._stores = stores if stores is not None else [get_vector_store()]

    async def index_existing_document(
        self, db: Session, document_id: str, *, reparse: bool = False
    ) -> int:
        """把一份已经登记在库的文档索引成切片，返回写入的切片数。

        ``reparse=True`` 强制重读源文件（忽略库内正文）；``allow_success=True`` 的
        抢占条件让「显式 force 重建」也能走到这里——路由层已经做过了 ``?force``
        守卫，这里只负责抢锁与执行。
        """

        doc = document_repository.get_by_id(db, document_id)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "document not found")

        # Tx1：抢占后立刻提交，只持锁一瞬间。抢不到说明另一个 Worker 已经在跑
        # 这份文档（重复投递 / 重试），直接跳过，绝不重复向量化。
        claimed = document_repository.try_claim_for_indexing(db, document_id, allow_success=True)
        db.commit()
        if not claimed:
            logger.info(
                "indexing skipped, another worker owns it",
                document_id=document_id,
                tenant_id=doc.tenant_id,
                kb_id=doc.kb_id,
            )
            return 0

        parsed = prepare_document_content(doc, reparse=reparse)

        pieces = self._splitter.split(parsed.content)
        if not pieces:
            raise AppError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "切块之后没有可用切片")

        texts = [piece.text for piece in pieces]
        vectors = await self._embeddings.embed_texts(texts)
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedding count mismatch: {len(texts)} texts -> {len(vectors)} vectors"
            )

        chunks: list[dict[str, Any]] = [
            {
                "id": new_chunk_id(),
                "tenant_id": doc.tenant_id,
                "kb_id": doc.kb_id,
                "document_id": doc.id,
                "content": piece.text,
                "embedding": vector,
                "meta": {
                    **piece.metadata,
                    "title": doc.title,
                    "index": index,
                    "source_type": parsed.source_type,
                },
            }
            for index, (piece, vector) in enumerate(zip(pieces, vectors))
        ]

        # Tx2：先删后写 + 状态回写，一次提交；任一步失败都由调用方回滚。
        await purge_document_chunks(db, document_id, self._stores)
        for store in self._stores:
            await store.add_chunks(db, chunks)

        document_repository.update_status(db, doc, DocumentStatus.SUCCESS)
        db.commit()
        logger.info(
            "document indexed",
            document_id=document_id,
            tenant_id=doc.tenant_id,
            kb_id=doc.kb_id,
            chunk_count=len(chunks),
        )
        return len(chunks)
