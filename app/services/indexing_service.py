"""索引流水线（Worker 侧）：解析 → 切块 → 拼检索文本 → 向量化 → 先删后写 → 状态回写。

上传请求线程只登记并投递任务，绝不进来这里；进程内的耗时（尤其是 embedding 的
网络往返）完全由 Celery Worker 承担。

**两段事务**是这里最重要的不变量：
1. ``try_claim_for_indexing`` 抢到索引权后立刻 ``commit``。若把整条流水线包在
   行锁里，``GET /documents`` 在 Worker 跑完之前都会卡在这份文档那一行上。
2. 解析 / 切块 / 向量化之后，purge（先删旧切片）+ 写入新切片 + ``status=SUCCESS``
   在同一段事务里提交：调用方永远不会看到「旧切片没了、新切片还没写」或
   「切片写好了、状态还是处理中」的中间态。异常时由调用方（Celery 任务）回滚。

**索引权只由 Worker 抢占。** 入队侧（重建端点 / 运维脚本）已经用
``prepare_for_indexing`` 把文档置成「已排队、未开工」（不盖租约），Worker 进来后
用 ``try_claim_for_indexing(allow_success=False)`` 盖租约。``allow_success=False``
是必须的：force 重建的路由已经做过 SUCCESS→PROCESSING 的状态准备，ACK 丢失导致的
迟到重复投递不能在 SUCCESS 之后重新抢占并重写切片。

并发去重靠数据库 CAS，不靠 Celery 的 ``task_id``：``acks_late`` 让投递至少一次，
而 ``try_claim_for_indexing`` 让重复投递里的后来者抢不到，直接返回 0。这两者是
配套的，不要只留一半。

所有依赖（切块器 / embedding / 存储列表）都从构造参数注入，任务里绝不在方法体
内 ``new`` 具体类：测试靠注入替身，整份测试不需要网络、Broker 或数据库。

**送进 embedding 的是检索文本，落库的 content 仍是切片原文。** 检索文本由
``build_retrieval_text`` 用「文档名 + 章节路径 + 正文」拼成，不落库：向量与
关键词索引共用这一份文本，两路对比才有意义；调用方拿到的 ``content``
必须还是原文（见 GLOSSARY 的「检索文本」与「切片原文」）。所以 chunk 字典里
同时带 ``content``（原文，落库）与 ``retrieval_text``（检索文本，进向量与关键词
索引），后者由向量存储忽略、由关键词 Provider 使用。

**双写遍历的是同一个列表。** 向量存储与关键词检索都满足
``app.services.chunk_store.ChunkStore`` 的写入契约，默认接线由
``build_chunk_stores`` 组装；加第三个存储只需要往列表里再加一个元素。索引阶段
不做降级：任一处写失败就是索引失败（由 Celery 任务回滚并置为失败），否则会留下
「向量库有、关键词库没有」的数据不一致。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.providers.embedding import EmbeddingProvider, get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import document_repository
from app.services.chunk_store import ChunkStore, build_chunk_stores
from app.services.document_ingestion import prepare_document_content
from app.utils.id_generator import new_chunk_id
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.retrieval_text import build_retrieval_text
from app.utils.status import DocumentStatus
from app.utils.text_splitter import TextSplitter

logger = structlog.get_logger(__name__)

# 索引链路失败时回传给调用方的唯一原因文案：不含异常对象、堆栈、路径或 SQL。
# 失败回写由 app.tasks.indexing._mark_failed 负责，它从这里 import；不要把异常
# 原文拼进来。
INDEXING_FAILED_MESSAGE = "document indexing failed, please retry or contact support"


async def purge_document_chunks(
    db: Session, document_id: str, *, tenant_id: str, stores: list[ChunkStore]
) -> None:
    """先删后写的第一半：遍历**存储列表**，逐个清除该文档的旧切片。

    遍历的是列表而不是写死某一个存储：向量存储与关键词检索实现同一个写入契约
    （见 ``app.services.chunk_store``），所以以后加第三个存储只需在默认接线里
    往 ``stores`` 追加一个元素，调用方一行都不用改。``tenant_id`` 是强制过滤参数
    （ADR-0006），连删除也必须带租户。
    """

    for store in stores:
        await store.delete_by_document_id(db, document_id, tenant_id=tenant_id)


class IndexingService:
    """Worker 侧的索引编排：一份已登记的文档 → 可检索的切片。"""

    def __init__(
        self,
        *,
        splitter: TextSplitter | None = None,
        embeddings: EmbeddingProvider | None = None,
        stores: list[ChunkStore] | None = None,
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
        # 默认接线就是双写：向量存储 + 关键词检索。没配关键词引擎时拿到的是空实现，
        # 它照常占一个位置（能力明确不可用，而不是「索引链路少接了一段」）。
        self._stores = (
            stores
            if stores is not None
            else build_chunk_stores(get_vector_store(), get_keyword_search_provider())
        )

    async def index_existing_document(
        self, db: Session, document_id: str, *, reparse: bool = False
    ) -> int:
        """把一份已经登记在库的文档索引成切片，返回写入的切片数。

        ``reparse=True`` 强制重解析原始文件（忽略库内正文）；这里固定
        ``allow_success=False``：force 重建的 SUCCESS→PROCESSING 状态准备已经在
        路由层用 ``prepare_for_indexing`` 完成，Worker 只负责抢租约与执行，迟到
        重复投递不能重新抢占成功文档。
        """

        doc = document_repository.get_by_id(db, document_id)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "document not found")

        # Tx1：抢占后立刻提交，只持锁一瞬间。抢不到说明另一个 Worker 已经在跑
        # 这份文档（重复投递 / 重试），直接跳过，绝不重复向量化。
        claimed = document_repository.try_claim_for_indexing(
            db, document_id, tenant_id=doc.tenant_id, allow_success=False
        )
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

        retrieval_texts = [
            build_retrieval_text(
                doc.title, str(piece.metadata.get("heading_path") or ""), piece.text
            )
            for piece in pieces
        ]
        vectors = await self._embeddings.embed_texts(retrieval_texts)
        if len(vectors) != len(retrieval_texts):
            raise ValueError(
                f"embedding count mismatch: {len(retrieval_texts)} texts -> {len(vectors)} vectors"
            )

        chunks: list[dict[str, Any]] = [
            {
                "id": new_chunk_id(),
                "tenant_id": doc.tenant_id,
                "kb_id": doc.kb_id,
                "document_id": doc.id,
                # content 是切片原文（落库 / 回给调用方）；retrieval_text 是检索文本
                # （进向量与关键词索引）。两者必须并存：向量存储只读 content，
                # 关键词 Provider 只读 retrieval_text。
                "content": piece.text,
                "retrieval_text": retrieval_texts[index],
                "embedding": vector,
                "index_version": DEFAULT_INDEX_VERSION,
                "meta": {
                    # 解析器产出的 metadata 必须跟着切片走，否则「元数据.engine 含 ocr」
                    # 这类验收根本无从查起（本步实测踩过）：显式合并，切块字段优先。
                    **parsed.metadata,
                    **piece.metadata,
                    "title": doc.title,
                    "index": index,
                    "source_type": parsed.source_type,
                },
            }
            for index, (piece, vector) in enumerate(zip(pieces, vectors))
        ]

        # Tx2：先删后写 + 状态回写，一次提交；任一步失败都由调用方回滚。
        await purge_document_chunks(db, document_id, tenant_id=doc.tenant_id, stores=self._stores)
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
