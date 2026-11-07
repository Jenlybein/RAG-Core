"""检索业务：一次检索走一条路，或两条路并行融合（票 4）。

边界：不拼上下文、不调大模型、不生成答案，只返回切片 + 分数 + 名次 + 出处。
第 1 步起 tenant_id 从占位 Key 解出，kb_ids 是调用方显式传的列表；
第 5 步会在这一层补 ``kb_ids ⊆ allowed_kb_ids`` 白名单校验。

**三种模式的边界（票 3 / 票 4 / 票 5）**：

* **可见性预校验**：``retrieve`` 在进入任何分支之前把 ``kb_ids`` 逐个校验一遍
  （N 个库 = N 次查询，票 5 之前是两条通道各查一遍 = 2N 次）。拒绝（20008）先于
  召回发生，且不参与降级。
* ``vector`` 先 ``embed_query``、再一次范围召回覆盖全部「库 + 版本」配对、按分数
  排序后截到 ``top_k``；单库时结果与逐库版一致，``score`` 仍是向量分（回归红线）。
* ``bm25`` 只走关键词一路：不向量化、不查向量库，``index_versions`` 是
  「kb_id → 索引版本」的映射（配对关系只能来自映射，不能是两个平行数组）。
* ``hybrid`` 两路并行召回，RRF 融合（融合与降级判定在
  ``app/services/retrieval/``，本模块只负责把结果翻译成响应契约）。
* **多库是一次全局召回**：两个通道各查一次，嵌入只算一次——分库各召回一批再
  合并是假均衡，不相关的库也各占名额。
* **「某库没有切片进入结果」不是失败**：不记 ``failed_kb_ids``，也不按结果条数
  推断某个库出了事；结果只按实际命中呈现。
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
from app.models.chunk import DEFAULT_INDEX_VERSION, default_index_versions
from app.providers.embedding import get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import chunk_repository
from app.services.knowledge_base_access import ensure_visible
from app.services.retrieval.fusion import with_score_columns
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

    # 可见性预校验：**进入任何分支之前**只做一遍。两条通道各校验一遍时，N 个库
    # 就是 2N 次查询；更重要的是「拒绝」会因此有两个来源、两条路径。放在这里之后，
    # 拒绝（20008）与被拒之后有没有真的发出召回无关——它先于召回发生（父 spec
    # 「降级矩阵」：任一知识库不可见 → 召回前拒绝）。被拒的请求连嵌入都不必算。
    for kb_id in kb_ids:
        ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)

    degraded = False
    degraded_reason: str | None = None
    rerank_info: dict[str, object] | None = None

    if resolved_mode == MODE_HYBRID:
        outcome = await hybrid_retrieve(
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            query=query,
            top_k=resolved_top_k,
            # 工厂以对象传入：测试替换工厂的位置因此仍只有一处（既有接缝）。
            embedding_factory=get_embedding_provider,
            vector_factory=get_vector_store,
            keyword_factory=get_keyword_search_provider,
            # 「召回宽、返回窄、融合池更宽」：三个深度都只在这里读配置，
            # 编排层不碰 settings。
            recall_depth=max(resolved_top_k, settings.HYBRID_RECALL_TOP_K),
            rerank_candidates=(
                settings.RERANK_MAX_CANDIDATES if settings.RERANK_ENABLED else None
            ),
        )
        chunks = outcome.chunks
        degraded = outcome.degraded
        degraded_reason = outcome.degraded_reason
        rerank_info = outcome.rerank
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
                # 重排事实（是否发生、候选数、返回数、耗时）。单模式下恒为 null：
                # 「重排」的定义是对**融合后**的候选重排，没有融合就没有这一步。
                "rerank": rerank_info,
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
    """向量通道：一次范围召回覆盖全部配对，**不按库循环、不按库分名额**。

    单库时就是一个配对条件，SQL 语义与逐库版一致（回归红线：顺序与打分口径不变）；
    多库时 ``top_k`` 是全局的——分库各取 top_k 再合并才是「假均衡」。
    """

    query_vector = await get_embedding_provider().embed_query(query)

    rows = list(
        await get_vector_store().similarity_search_scope(
            db,
            query_vector,
            tenant_id=tenant_id,
            index_versions=default_index_versions(kb_ids),
            top_k=top_k,
        )
    )

    # 多库时按分数重新排，再截到 top_k；单库行为与骨架期一致。
    rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return [
        # 五列契约字段统一由融合层的 ``with_score_columns`` 补齐：单模式下只有本通道
        # 那一路是实值，另一路是 ``None``（不是 0），融合分为 ``None``（没有融合）。
        with_score_columns(row, vector_score=row.get("score"), vector_rank=rank)
        for rank, row in enumerate(rows[:top_k], start=1)
    ]


async def _retrieve_by_keyword(
    db: Session, *, tenant_id: str, kb_ids: list[str], query: str, top_k: int
) -> list[dict]:
    """关键词通道：租户 + 库/版本配对过滤，纯关键词打分。

    可见性不在这里校验：``retrieve`` 已经在进入任何分支之前统一校验过一遍
    （ADR-0006 的先后顺序不变：不可见的库先被拒绝）。关键词引擎的可用性检查
    仍在这里做——空实现是「能力不存在」，不是「查过了没命中」。
    """

    try:
        provider = get_keyword_search_provider()
        if not provider.available:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured"
            )
        rows = await provider.keyword_search(
            query,
            tenant_id=tenant_id,
            index_versions=default_index_versions(kb_ids),
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
        with_score_columns(
            {
                "document_id": row["document_id"],
                "chunk_id": row["chunk_id"],
                "title": row.get("title", ""),
                "content": row.get("content", ""),
                "kb_id": row.get("kb_id"),
                "score": row.get("score", 0.0),
            },
            bm25_score=row.get("score", 0.0),
            bm25_rank=rank,
        )
        for rank, row in enumerate(rows[:top_k], start=1)
    ]
