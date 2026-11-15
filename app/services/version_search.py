"""按版本只读直查：在某篇文档的**某一稿**上检索，什么都不改（#176）。

它是排查「旧版明明写了、新版为什么召回不到」的唯一工具。因此这个模块的契约是**窄**的，
而且窄得有理由：

## 只读

不改当前稿指针、不重建、**不写归属索引**（``retrieval_traces``）。一次排查不是一次检索：
写进去会让「一次 Research」在统计里多出几行，而调用方完全看不出这件事。

## 锁定「某篇某稿」，绕过「只看当前稿」

向量侧的默认召回带 ``current_draft_filter``（``chunks.doc_version =
documents.current_version``，唯一的「当前稿」谓词）。历史稿的切片与指针不相等，所以直查
必须走 :meth:`VectorStore.similarity_search_document_version` —— 那条路按
``(document_id, doc_version, index_version)`` 收窄、不带当前稿谓词。**默认路径的语义一个字
没改**：不传稿号时检索仍只看当前稿。

## 只走向量路（边界，写在这里免得被误解）

关键词索引里只有 ``is_current`` **一个布尔**：历史稿切片的标记是 ``False``，按它过滤就是
空结果。要让关键词路也支持「查某一稿」，得给 ES 的查询体再加一个「指定稿号」谓词——那超出
本票范围（#176 只做向量路直查）。

所以：**直查不等于混合检索**。回包里没有 BM25 分数，代码里也不 import 关键词引擎
（``tests/test_document_version_search.py`` 用 import 表钉住这条边界）。要排查「关键词路为
什么召回不到旧稿」，只能看当前稿的关键词结果，不能靠这条入口。

## 失败语义

向量引擎故障属于「外部服务请求失败」（``50002``），不是 500：与关键词单模式的分类同一条
口径（``app/services/retrieval/kernel.py``）。日志只记类型与范围，不记问句原文，也不把
上游响应体带出去（那里可能有 mapping 细节与内部字段名）。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding import get_embedding_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import (
    document_repository,
    document_version_repository,
    knowledge_base_repository,
)

logger = structlog.get_logger(__name__)

#: 直查的条数：这是排查工具，不是交付链路，条数写死在这里而不是请求参数。
#:
#: 与 ``/rag/retrieve`` 的理由相同（ADR-0010：质量参数不是请求参数）：一个「查几条」的旋钮
#: 会让两个人的排查结论不可比。10 足够回答「这一稿里到底有没有这段内容」。
VERSION_SEARCH_TOP_K = 10


async def search_document_version(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    version: int,
    query: str,
) -> dict:
    """在 ``document_id`` 的第 ``version`` 稿上跑一次只读向量检索。

    四步，顺序是契约：

    1. **按 ``(id, tenant_id)`` 取文档**：缺失与跨租户统一「不存在」（20016），不泄露存在性；
    2. **这一稿存在吗**：版本行读不到就是「稿不存在」（20030）。保留策略物理删除过的稿
       也走这一条——不做墓碑，所以「查一个删掉的稿」与「查一个从没存在过的稿号」同一个答案；
    3. **索引版本**：直查也要落在**线上那一版**上（旧索引版本的行还在库里），版本由库行
       解析、不是请求参数；
    4. **向量路召回**：一次嵌入 + 一次锁定稿号的查询。

    回包带 ``is_current``：调用方要能一眼看出「查的这稿是不是当前稿」。它不是权限也不是
    开关，只是让排查的人知道结果代表哪个时刻。

    读路径**不提交事务**：本函数一次写都不做，会话由调用方管理（路由层因此不声明事务依赖）。
    """

    doc = await document_repository.get_by_id_for_tenant_async(
        db, document_id, tenant_id=tenant_id
    )
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND)

    row = await document_version_repository.get_by_version(db, document_id, version)
    if row is None:
        raise AppError(
            ErrorCode.DOCUMENT_VERSION_NOT_FOUND,
            "要查的稿次不存在",
            data={"version": version},
        )

    index_version = await knowledge_base_repository.get_effective_index_version(
        db, doc.kb_id, tenant_id=tenant_id
    )

    try:
        vector = await get_embedding_provider().embed_query(query)
        rows = await get_vector_store().similarity_search_document_version(
            db,
            vector,
            tenant_id=tenant_id,
            document_id=document_id,
            doc_version=version,
            index_version=index_version,
            top_k=VERSION_SEARCH_TOP_K,
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 外部服务失败统一成 50002，见模块文档
        # 只记类型与范围：异常原文可能带着上游响应体（mapping 细节、内部字段名）。
        logger.warning(
            "version search failed",
            document_id=document_id,
            tenant_id=tenant_id,
            doc_version=version,
            error=type(exc).__name__,
        )
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "version search failed") from exc

    return {
        "document_id": document_id,
        "version": version,
        "is_current": version == doc.current_version,
        "query": query,
        "retrieved_chunks": [
            {
                "chunk_id": item["chunk_id"],
                "content": item["content"],
                "score": float(item["score"]),
                "doc_version": int(item["doc_version"]),
            }
            for item in rows
        ],
    }


__all__ = ["VERSION_SEARCH_TOP_K", "search_document_version"]
