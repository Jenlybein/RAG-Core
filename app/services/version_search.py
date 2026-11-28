"""按版本只读直查：在某篇文档的**某一稿**上检索，什么都不改（#176）。

它是排查「旧版明明写了、新版为什么召回不到」的唯一工具。因此这个模块的契约是**窄**的，
而且窄得有理由：

## 只读

不改当前稿指针、不重建。**写账是另一件事，而且是必须的**（#208）：本模块提供
:func:`record_version_search`，由调用方在每次直查结束后补一行 ``usage_events``。
早先这里写的是「**不写归属索引**（``usage_events``）」，理由是「一次排查不是一次检索：
写进去会让『一次 Research』在统计里多出几行」——**这条理由仍然成立**，所以那行账记的是
``endpoint=version_search``、**三本账全 0**（它不扣任何额度、也不过配额门禁），只是
让「谁在什么时候拿 Key 白调了一次 embedding」有据可查。留痕与扣费是两件事。

本模块自己**仍然一次写都不做**：``search_document_version`` 是纯读，记账放在它外面
（路由层与服务自己各补一次，见 :func:`record_version_search` 的文档）。

## 锁定「某篇某稿」，绕过「只看当前稿」

向量侧的默认召回带 ``current_draft_filter``（``chunks.doc_version =
documents.current_version``，唯一的「当前稿」谓词）。历史稿的切片与指针不相等，所以直查
必须走 :meth:`VectorStore.similarity_search_document_version` —— 那条路按
``(document_id, doc_version, generation)`` 收窄、不带当前稿谓词。**默认路径的语义一个字
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

import time
from collections.abc import Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.providers.embedding import get_embedding_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import (
    chunk_repository,
    document_repository,
    document_version_repository,
)
from app.services.retrieval_trace import record_retrieval_trace
from app.services.trace_identity import trace_id_for
from app.services.usage_meter import ENDPOINT_VERSION_SEARCH
from app.utils.id_generator import new_retrieval_log_id

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
    key_id: str | None = None,
) -> dict:
    """在 ``document_id`` 的第 ``version`` 稿上跑一次只读向量检索。

    四步，顺序是契约：

    1. **按 ``(id, tenant_id)`` 取文档**：缺失与跨租户统一「不存在」（20016），不泄露存在性；
    2. **这一稿存在吗**：版本行读不到就是「稿不存在」（20030）。保留策略物理删除过的稿
       也走这一条——不做墓碑，所以「查一个删掉的稿」与「查一个从没存在过的稿号」同一个答案；
    3. **产物代**：这一稿**现存最高的代**从切片表算出来（同一稿可能有旧代残留），不是
       请求参数——库级「生效版本」那一层已经删除，可见性只剩「篇级当前代 × 当前稿」；
    4. **向量路召回**：一次嵌入 + 一次锁定稿号的查询。

    回包带 ``is_current``：调用方要能一眼看出「查的这稿是不是当前稿」。它不是权限也不是
    开关，只是让排查的人知道结果代表哪个时刻。

    读路径**不提交事务**：``search_document_version`` 一次写都不做，会话由调用方管理
    （路由层因此不声明事务依赖）。**但每次调用都补一行账**（#208）——写账走
    ``record_retrieval_trace`` 的独立短事务，失败只留 warning，不影响这次直查的返回。
    三本账全 0：它不扣额度、也不过配额门禁，留痕只为可统计。
    """

    started_at = time.perf_counter()
    doc = await document_repository.get_by_id_for_tenant_async(db, document_id, tenant_id=tenant_id)
    if doc is None:
        await record_version_search(
            tenant_id=tenant_id,
            key_id=key_id,
            document_id=document_id,
            version=version,
            kb_id=None,
            chunk_ids=(),
            latency_ms=_elapsed_ms(started_at),
            code=ErrorCode.NOT_FOUND,
        )
        raise AppError(ErrorCode.NOT_FOUND)

    row = await document_version_repository.get_by_version(db, document_id, version)
    if row is None:
        await record_version_search(
            tenant_id=tenant_id,
            key_id=key_id,
            document_id=document_id,
            version=version,
            kb_id=doc.kb_id,
            chunk_ids=(),
            latency_ms=_elapsed_ms(started_at),
            code=ErrorCode.DOCUMENT_VERSION_NOT_FOUND,
        )
        raise AppError(
            ErrorCode.DOCUMENT_VERSION_NOT_FOUND,
            "要查的稿次不存在",
            data={"version": version},
        )

    # 这一稿**现存最高的代**（现场九·第四步）：当前稿就是当前代；历史稿则是最后一回把它
    # 算出来的那一代。改造前这里按库级生效版本收窄——版本不再决定可见性之后，那样查会把
    # 同一稿并存的旧代混进来（旧切法的片可能挤掉新的，结论变成「这一稿里就是这些内容」）。
    generation = await chunk_repository.max_generation_for_draft(
        db, document_id, tenant_id=tenant_id, doc_version=version
    )
    # 没有产物时**不发任何检索语句**：一个常量条件换不回东西，而空命中是诚实答案。
    rows: list[dict] = []
    if generation is not None:
        try:
            vector = await get_embedding_provider().embed_query(query)
            rows = await get_vector_store().similarity_search_document_version(
                db,
                vector,
                tenant_id=tenant_id,
                document_id=document_id,
                doc_version=version,
                generation=generation,
                top_k=VERSION_SEARCH_TOP_K,
            )
        except AppError as exc:
            await record_version_search(
                tenant_id=tenant_id,
                key_id=key_id,
                document_id=document_id,
                version=version,
                kb_id=doc.kb_id,
                chunk_ids=(),
                latency_ms=_elapsed_ms(started_at),
                code=exc.code,
            )
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
            await record_version_search(
                tenant_id=tenant_id,
                key_id=key_id,
                document_id=document_id,
                version=version,
                kb_id=doc.kb_id,
                chunk_ids=(),
                latency_ms=_elapsed_ms(started_at),
                code=ErrorCode.RETRIEVAL_FAILED,
            )
            raise AppError(ErrorCode.RETRIEVAL_FAILED, "version search failed") from exc

    chunk_ids = [str(item["chunk_id"]) for item in rows]
    await record_version_search(
        tenant_id=tenant_id,
        key_id=key_id,
        document_id=document_id,
        version=version,
        kb_id=doc.kb_id,
        # 归属索引的 ``index_generations`` 留空映射（理由见 :func:`record_version_search`）；
        # ``chunk_ids`` 照写：排查要能回答「这次命中了哪几片」，它进的是归属索引的
        # ``chunk_ids`` 那一列。
        chunk_ids=chunk_ids,
        latency_ms=_elapsed_ms(started_at),
        code=0,
    )

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


def _elapsed_ms(started_at: float) -> int:
    """这次直查的墙钟耗时（毫秒）。整数毫秒：账本里的列就是整数。"""

    return int((time.perf_counter() - started_at) * 1000)


async def record_version_search(
    *,
    tenant_id: str,
    key_id: str | None,
    document_id: str,
    version: int,
    kb_id: str | None,
    chunk_ids: Sequence[str],
    latency_ms: int,
    code: int,
) -> None:
    """把一次直查写进 ``usage_events``：**一次调用一行**，三本账全 0（#208）。

    为什么排查也要留一行：这条入口真的调一次 ``embed_query``（有模型成本），而此前账上
    没有任何一行能回答「谁在拿 Key 白用它」。它**不扣任何额度**——三本账全 0、也不过配额
    门禁——所以把它记成一次 ``retrieve`` 才会污染统计；记成自己的 ``version_search`` 不会。

    归属索引的 ``index_generations`` 留**空映射**：直查落在「该稿现存最高的代」上——当前稿时
    它恰好等于当前代，历史稿时却是最后一回算出它的那一代。把后者写进
    ``index_generations`` 会让读侧把它当「这一篇的当前代」，那是静默错归因；要查「查了哪一稿」
    读 ``doc_versions`` 那一列。

    失败也要记：``code`` 是这次的真实结局（``0`` = 成功，其余是业务码）。写账走
    ``record_retrieval_trace`` 的独立短事务，任何失败只降级成一条 warning——账本坏了
    不该让一次排查问不出来。
    """

    log_id = new_retrieval_log_id()
    await record_retrieval_trace(
        log_id=log_id,
        trace_id=trace_id_for(log_id),
        tenant_id=tenant_id,
        key_id=key_id,
        endpoint=ENDPOINT_VERSION_SEARCH,
        code=code,
        # 三本账全 0：它没有扣任何额度（不过配额门禁）。
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
        kb_ids=[kb_id] if kb_id else [],
        # 空映射的理由见上：直查的代数轴是「该稿现存最高代」，不是「篇的当前代」。
        index_generations={},
        doc_versions={document_id: version},
        chunk_ids=list(chunk_ids),
        # 缺代计数是**检索范围**的事（「范围内有几篇还没有产物」），而直查的范围是**一篇**、
        # 且答案恒定（能查到稿就说明这一篇有产物）。所以这一列恒 0，也不发那条计数查询。
        missing_generation_documents=0,
        top_k=VERSION_SEARCH_TOP_K,
        latency_ms=latency_ms,
        degraded_reason=None,
        # 空结果不编原因：直查回答的是「这一稿里有没有这段内容」，两种空对排查者是同一个
        # 答案；要区分得再查一次该稿的切片数，那是另一次的代价。
        empty_reason=None,
    )


__all__ = ["VERSION_SEARCH_TOP_K", "record_version_search", "search_document_version"]
