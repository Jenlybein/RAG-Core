"""检索业务：一次检索走一条路，或两条路并行融合（票 4）。

边界：不拼上下文、不调大模型、不生成答案，只返回切片 + 分数 + 名次 + 出处。
第 1 步起 tenant_id 从占位 Key 解出，kb_ids 是调用方显式传的列表；
第 5 步会在这一层补 ``kb_ids ⊆ allowed_kb_ids`` 白名单校验。

**三种模式的边界（票 3 / 票 4）**：

* ``vector`` 与之前完全一致：先 ``embed_query``、再逐库校验可见性并召回、
  按分数合并排序后截到 ``top_k``；``score`` 仍是向量分（回归红线）。
* ``bm25`` 只走关键词一路：不向量化、不查向量库，``index_versions`` 是
  「kb_id → 索引版本」的映射（配对关系只能来自映射，不能是两个平行数组）。
* ``hybrid`` 两路并行召回，RRF 融合（融合与降级判定在
  ``app/services/retrieval/``，本模块只负责把结果翻译成响应契约）。
* **单模式不降级**：关键词引擎没配或调用失败一律 ``RETRIEVAL_FAILED``（50002），
  绝不偷偷退回向量——调用方选 bm25 往往就是在评测单路效果。
* **混合模式对称降级**：哪一路挂了都用另一路的结果，``code`` 仍为 0，
  ``metadata.retrieval.degraded`` 与 ``degraded_reason`` 如实说明；两路都挂才拒绝。

**三种结局**（父 spec「降级矩阵」）：拒绝（业务错误）/ 降级（成功 + 标记）/
诚实空（成功 + ``empty_reason``）。空结果不是故障：把它当故障处理会让监控
满地假警报，也让调用方误以为该重试。

日志只放 ID 与派生量，绝不放检索原文（见 tests/test_retrieval_log_hygiene.py）。
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import DEFAULT_INDEX_VERSION
from app.providers.embedding import get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import chunk_repository
from app.services.knowledge_base_access import ensure_visible
from app.services.retrieval.pipeline import hybrid_retrieve

logger = structlog.get_logger(__name__)

MODE_VECTOR = "vector"
MODE_BM25 = "bm25"
MODE_HYBRID = "hybrid"

# 空结果的两种原因：调用方据此决定「去传文档」还是「换个问法」。
EMPTY_NO_INDEXED_CHUNKS = "no_indexed_chunks"
EMPTY_NO_CHUNKS_MATCHED = "no_chunks_matched"


async def retrieve(
    db: Session,
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    mode: str | None = None,
    top_k: int | None = None,
) -> dict:
    """按 ``mode`` 走一条通道（或并行两路）；缺省值取服务端配置。

    MCP 入口只传 ``query`` / ``kb_ids``，因此它的行为随服务端默认值变化
    （票 4 起默认 ``hybrid``）——这正是「两个入口共用同一份业务」的体现。
    """

    resolved_mode = mode or settings.RETRIEVAL_MODE
    resolved_top_k = top_k or settings.TOP_K

    degraded = False
    degraded_reason: str | None = None

    if resolved_mode == MODE_HYBRID:
        outcome = await hybrid_retrieve(
            db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            query=query,
            top_k=resolved_top_k,
            # 工厂以对象传入：测试替换工厂的位置因此仍只有一处（既有接缝）。
            embedding_factory=get_embedding_provider,
            vector_factory=get_vector_store,
            keyword_factory=get_keyword_search_provider,
            # 「召回宽、返回窄」：只在这里读配置，编排层不碰 settings。
            recall_depth=max(resolved_top_k, settings.HYBRID_RECALL_TOP_K),
        )
        chunks = outcome.chunks
        degraded = outcome.degraded
        degraded_reason = outcome.degraded_reason
    elif resolved_mode == MODE_BM25:
        chunks = await _retrieve_by_keyword(
            db, tenant_id=tenant_id, kb_ids=kb_ids, query=query, top_k=resolved_top_k
        )
    elif resolved_mode == MODE_VECTOR:
        chunks = await _retrieve_by_vector(
            db, tenant_id=tenant_id, kb_ids=kb_ids, query=query, top_k=resolved_top_k
        )
    else:
        # HTTP 入口的 schema 已经用 Literal 掐掉了未知值；MCP / 内部调用走这里。
        raise AppError(ErrorCode.PARAM_ERROR, f"unsupported retrieval mode: {resolved_mode!r}")

    return {
        "query": query,
        "kb_ids": list(kb_ids),
        "retrieved_chunks": chunks,
        "metadata": {
            "mode": resolved_mode,
            "top_k": resolved_top_k,
            "vector_store": settings.VECTOR_STORE,
            # 降级与空结果统一挂在这一层（父 spec「响应契约」）：调用方读一个地方
            # 就能判断「这是不是完整结果」「为什么是空的」。
            "retrieval": {
                "mode": resolved_mode,
                "degraded": degraded,
                "degraded_reason": degraded_reason,
                "empty_reason": _empty_reason(db, tenant_id=tenant_id, kb_ids=kb_ids, chunks=chunks),
            },
        },
    }


def _empty_reason(
    db: Session, *, tenant_id: str, kb_ids: list[str], chunks: list[dict]
) -> str | None:
    """空结果的原因：查一次 count，绝不按结果条数猜。

    * ``no_indexed_chunks``：这些库里一条可检索切片都没有（该去传文档）；
    * ``no_chunks_matched``：库里有内容，但本次没有命中（该换个问法）。

    有结果时返回 ``None``——``empty_reason`` 只在真为空时出现，不是一个常驻字段。
    """

    if chunks:
        return None
    indexed = chunk_repository.count_by_kb_ids(
        db,
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        # 必须和召回时的过滤口径一致：库里可能有别的索引版本的切片，但它们对这次
        # 检索不可见。不过滤版本会让「本该叫用户换问法」的库被误判成「空库」。
        index_version=DEFAULT_INDEX_VERSION,
    )
    return EMPTY_NO_INDEXED_CHUNKS if indexed == 0 else EMPTY_NO_CHUNKS_MATCHED


async def _retrieve_by_vector(
    db: Session, *, tenant_id: str, kb_ids: list[str], query: str, top_k: int
) -> list[dict]:
    """向量通道：顺序与打分口径与本票之前一致（回归红线，别动）。"""

    query_vector = await get_embedding_provider().embed_query(query)

    rows: list[dict] = []
    for kb_id in kb_ids:
        ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)
        for row in await get_vector_store().similarity_search(
            db, query_vector, tenant_id=tenant_id, kb_id=kb_id, top_k=top_k
        ):
            # kb_id 由编排层补上：跨库结果必须能归属到某个库。
            rows.append({**row, "kb_id": kb_id})

    # 多库时按分数重新排，再截到 top_k；单库行为与骨架期一致。
    rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return [
        {
            **row,
            "vector_score": row.get("score"),
            "bm25_score": None,
            "vector_rank": rank,
            "bm25_rank": None,
            "rrf_score": None,
        }
        for rank, row in enumerate(rows[:top_k], start=1)
    ]


async def _retrieve_by_keyword(
    db: Session, *, tenant_id: str, kb_ids: list[str], query: str, top_k: int
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤，纯关键词打分。

    可见性校验放在召回之前（与向量通道同一条 ADR-0006 约束）；关键词引擎的
    可用性检查也在这里做——空实现是「能力不存在」，不是「查过了没命中」。
    """

    for kb_id in kb_ids:
        ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)

    try:
        provider = get_keyword_search_provider()
        if not provider.available:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured"
            )
        rows = await provider.keyword_search(
            query,
            tenant_id=tenant_id,
            index_versions={kb_id: DEFAULT_INDEX_VERSION for kb_id in kb_ids},
            top_k=top_k,
        )
    except AppError:
        raise
    except Exception as exc:
        # 引擎故障属于「这一路不可用」，不是整单 500：错误分类固定为 50002。
        # 日志只记类型与范围，不记检索原文。
        logger.warning(
            "keyword search failed",
            tenant_id=tenant_id,
            kb_ids=list(kb_ids),
            error=type(exc).__name__,
        )
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search failed") from exc

    return [
        {
            "document_id": row["document_id"],
            "chunk_id": row["chunk_id"],
            "title": row.get("title", ""),
            "content": row.get("content", ""),
            "kb_id": row.get("kb_id"),
            "score": row.get("score", 0.0),
            "vector_score": None,
            "bm25_score": row.get("score", 0.0),
            "vector_rank": None,
            "bm25_rank": rank,
            "rrf_score": None,
        }
        for rank, row in enumerate(rows[:top_k], start=1)
    ]
