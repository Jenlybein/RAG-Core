"""检索业务：一次检索走一条路，或两条路并行融合（票 4）。

边界：不拼上下文、不生成答案，只返回切片 + 分数 + 名次 + 出处。``tenant_id`` 来自密钥
解析（数据面的身份只有一份判定，见 ``app.core.auth``），``kb_ids`` 是调用方显式传的列表。
``kb_ids ⊆ allowed_kb_ids`` 的**白名单关卡不在这里**：它是六道关卡的第二道，由两个入口
（HTTP 路由与 MCP 工具）在进本函数之前调 ``app.services.data_scope.ensure_kb_scope`` 判掉；
本模块只负责「租户对这几个库可不可见」（``ensure_visible``）与检索链路本身。

**唯一的大模型调用是查询改写**（票 6），而且它由 ``app/services/query/`` 负责、失败即
降级：本模块只决定「哪一路拿哪一句」，不认识模型、不认识提示词。

**三种模式的边界（票 3 / 票 4 / 票 5）**：

* **可见性预校验**：``retrieve`` 在进入任何分支之前把 ``kb_ids`` 逐个校验一遍
  （N 个库 = N 次查询，票 5 之前是两条通道各查一遍 = 2N 次）。拒绝（20008）先于
  召回发生，且不参与降级。它返回的**库行**顺着往下传：票 6 要用它们的 ``settings``
  （同义词表）与名称 / 描述（改写提示），再查一次库就等于每库两次。
* **查询优化在召回之前**（票 6 / #31）：``vector_queries`` 给向量一路逐句召回
  （改写成功 = 原句 + 改写句，关闭 / 失败 = 只有原句），``effective_query`` 给重排，
  ``search_query`` 给关键词一路。两个开关默认关闭，关闭时这一步是恒等变换。
* ``vector`` 按 ``vector_queries`` 逐句 ``embed_query``、逐句一次范围召回覆盖全部
  「库 + 版本」配对，并成一个池后按分数排序截到 ``top_k``；关闭改写时只有一句，
  调用次数、顺序与 ``score`` 口径全部与票 4 逐字节一致（回归红线）。
* ``bm25`` 只走关键词一路：不向量化、不查向量库，``index_versions`` 是
  「kb_id → 索引版本」的映射（配对关系只能来自映射，不能是两个平行数组）。
* ``hybrid`` 两路并行召回，RRF 融合（融合与降级判定在
  ``app/services/retrieval/``，本模块只负责把结果翻译成响应契约）。
* **多库是每个查询一次全局召回**：关键词一路查一次；向量一路按 ``vector_queries``
  逐句各查一次再并成一个池（#31），嵌入次数 = 句子数——分库各召回一批再合并是
  假均衡，不相关的库也各占名额。
* **「某库没有切片进入结果」不是失败**：不记 ``failed_kb_ids``，也不按结果条数
  推断某个库出了事；结果只按实际命中呈现。
* **单模式不降级**：关键词引擎没配或调用失败一律 ``RETRIEVAL_FAILED``（50002），
  绝不偷偷退回向量——调用方选 bm25 往往就是在评测单路效果。
* **混合模式对称降级**：哪一路挂了都用另一路的结果，``code`` 仍为 0，
  ``metadata.retrieval.degraded`` 与 ``degraded_reason`` 如实说明；两路都挂才拒绝。
* **降级原因一因一码、先到的才算根因**：改写发生在召回之前，所以它先失败时，通道
  故障与重排失败都不覆盖它。

**三种结局**（父 spec「降级矩阵」）：拒绝（业务错误）/ 降级（成功 + 标记）/
诚实空（成功 + ``empty_reason``）。空结果不是故障：把它当故障处理会让监控
满地假警报，也让调用方误以为该重试。

**质量参数从哪来（第 7 步，#77 / #78）**：请求体只有 ``kb_ids`` + ``query``；模式、返回
条数、重排、改写、折叠与表格展开由平台层从密钥配置解析、并过完五道门禁
（``app.services.profile_policy``）后交成一个 :class:`RetrievalOptions` 进来。本模块因此
**不再认识请求模型**，也不再为任何一个参数写「没传就取缺省」的分支；两个布尔开关
（``options.rerank`` / ``options.rewrite``）同理——它们是**生效值**（档位意图 ∧ 本机
能力），本模块不再自己去问一次 ``settings``。

**每次响应都带 ``tenant_policy``**：套餐名、要求的档位、实际模式、条数、重排、改写、
本次用到的库数、被裁字段。它从**同一个** ``options`` 上读出（``tenant_policy_echo``），
所以「回显说开了重排」与「链路真的开了重排」不可能分叉——这份回显不是另存的一份事实。

**每次响应都带 ``trace_id``，而且它先于写库产生**（#101，ADR-0010「第五次修订」）：
``log_id`` 先生成（UUID v4），``trace_id`` 立刻由它确定性派生（``trace_identity``），
然后才在最后尝试往归属索引里写一行结构记录。顺序不能倒：写入是旁路，写失败只留 warning，
响应里的 ``trace_id`` 照样有值——「写成功才拿到 id」等于把契约挂在旁路的健康度上。

除 ``metadata.trace_id`` 之外，检索响应逐字节不变；阶段耗时**不进**响应（那是观测的事）。

日志只放 ID 与派生量，绝不放检索原文（见 tests/test_retrieval_log_hygiene.py）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import structlog

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.chunk import DEFAULT_INDEX_VERSION, default_index_versions
from app.providers.embedding import get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import chunk_repository
from app.services import result_collapse
from app.services.knowledge_base_access import ensure_visible
from app.services.query.optimizer import optimize_query
from app.services.query.synonyms import collect_synonym_groups
from app.services.retrieval.pipeline import hybrid_retrieve
from app.services.retrieval.vector_recall import vector_recall
from app.services.retrieval_options import RetrievalOptions, tenant_policy_echo
from app.services.retrieval_trace import record_retrieval_trace
from app.services.trace_identity import delivered_chunk_ids, trace_id_for
from app.utils.id_generator import new_retrieval_log_id
from app.utils.retrieval_fields import with_score_columns

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
    options: RetrievalOptions,
) -> dict:
    """按 ``options.mode`` 走一条通道（或并行两路）。

    ``options`` 是**已经解析完**的本次参数（模式、返回条数、重排、改写、两个结果形状
    开关），由平台层过完门禁后交进来（``app.services.profile_policy``）——请求体里只剩
    ``kb_ids`` + ``query``，质量参数没有第二个来源。

    本函数仍是**组合根**：它继续读那些**不是每次调用都能选**的服务端旋钮（召回深度
    ``HYBRID_RECALL_TOP_K``、重排候选池 ``RERANK_MAX_CANDIDATES``），并继续校验模式
    ——MCP 与内部调用仍可能自己造一个 ``options``，模式词表那一道不能因为有了值对象
    就省掉。

    **重排与改写的开关读 ``options``，不读 ``settings``**：这两个布尔已经是「档位想要 ∧
    本机装了」的合取（``app.services.retrieval_options.options_from_chain``）。链路里
    再问一次 ``settings`` 就等于有了两个真相——机器上没装重排、档位却想要，两处判断
    谁说了算会变成排障时的第一道谜题。
    """

    resolved_mode = options.mode
    resolved_top_k = options.top_k

    # ── 身份先于旁路：id 与 trace_id 在任何一次写库尝试**之前**就有值 ──
    # 计时也从这里开始：``latency_ms`` 要的是「这次检索多慢」，不是「写完日志多慢」。
    started_at = time.perf_counter()
    # ``log_id`` 是这张表的对外身份（响应里不出现），``trace_id`` 由它确定性派生。
    # 两行代码写在一起是刻意的：换掉派生方式或换掉种子，都会同时破掉「同一 log_id 恒等」
    # 与「库里那一行能和响应那根线对上」这两条性质。
    log_id = new_retrieval_log_id()
    trace_id = trace_id_for(log_id)

    # 折叠会把同一父节的多个命中并成一条，所以召回要多取几倍，否则去重后凑不够
    # top_k 条。关掉折叠时按原样召回——「不折叠」就该连多取也不要。
    recall_top_k = (
        resolved_top_k * result_collapse.OVERFETCH_FACTOR
        if options.fold_to_sections
        else resolved_top_k
    )

    # 模式校验放在最前面：两个入口的解析（``profile_policy.resolve_retrieval_options``）已经
    # 验过词表，只有直接构造 ``options`` 的内部调用才会带着未知值到这里。校验先于可见性
    # 校验与查询优化，被拒的请求不该白烧一次改写（与「拒绝先于召回」同一条理由）。
    if resolved_mode not in (MODE_HYBRID, MODE_BM25, MODE_VECTOR):
        raise AppError(ErrorCode.PARAM_ERROR, f"unsupported retrieval mode: {resolved_mode!r}")

    # 可见性预校验：**进入任何分支之前**只做一遍。两条通道各校验一遍时，N 个库
    # 就是 2N 次查询；更重要的是「拒绝」会因此有两个来源、两条路径。放在这里之后，
    # 拒绝（20008）与被拒之后有没有真的发出召回无关——它先于召回发生（父 spec
    # 「降级矩阵」：任一知识库不可见 → 召回前拒绝）。被拒的请求连嵌入都不必算。
    #
    # 顺手把这几个库的**行**留下来：票 6 起要用它们的 ``settings``（同义词表）与
    # 名称 / 描述（改写提示）。``ensure_visible`` 本来就把整行查出来了，丢掉它再去
    # 查一次库，就把「每库一次」变成「每库两次」。
    visible_kbs = [ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id) for kb_id in kb_ids]

    # 查询优化：召回之前把用户那句话加工成「向量逐句列表 + 关键词一句 + 重排一句」
    # （GLOSSARY「检索链路」：查询优化 → 召回 → 融合 → 重排）。改写开关读 ``options``：
    # 它是「档位想要 ∧ 本机装了」的合取，关闭时这一步是恒等变换。
    optimized = await optimize_query(
        query=query,
        # 词表是**库级配置**：只合并本次涉及的库，不跨库、不跨租户。
        synonym_groups=collect_synonym_groups(
            [getattr(kb, "settings", None) for kb in visible_kbs]
        ),
        rewrite_hint=_rewrite_hint(visible_kbs),
        rewrite_enabled=options.rewrite,
    )

    degraded = optimized.rewrite_degraded
    degraded_reason: str | None = optimized.rewrite_reason
    rerank_info: dict[str, object] | None = None

    if resolved_mode == MODE_HYBRID:
        outcome = await hybrid_retrieve(
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            # 向量一路逐句都搜（改写成功时原句 + 改写句）；重排仍看 effective_query；
            # 关键词一路仍拿扩展后的句子，完全不变（#31）。
            vector_queries=optimized.vector_queries,
            rerank_query=optimized.effective_query,
            keyword_query=optimized.search_query,
            top_k=recall_top_k,
            # 工厂以对象传入：测试替换工厂的位置因此仍只有一处（既有接缝）。
            embedding_factory=get_embedding_provider,
            vector_factory=get_vector_store,
            keyword_factory=get_keyword_search_provider,
            # 「召回宽、返回窄、融合池更宽」：三个深度都只在这里读配置，
            # 编排层不碰 settings。融合池宽度只在**这次要重排**时才加宽——不重排时
            # 让它等于 top_k，与加宽之前逐字节一致（候选多了也不会有下次重排去挑）。
            recall_depth=max(resolved_top_k, settings.HYBRID_RECALL_TOP_K),
            rerank_candidates=settings.RERANK_MAX_CANDIDATES if options.rerank else None,
            # 「这次要不要重排」由本次的已解析参数说了算：装了重排的机器上，不要重排
            # 的那一档也不该被顺手排一遍（pipeline 里靠 provider 可用性判断会把
            # 「本次不要」静默变成「本次跑了」）。
            rerank_enabled=options.rerank,
        )
        chunks = outcome.chunks
        # 一因一码，先到的才是根因：改写发生在召回之前，所以它已经在场时通道故障与
        # 重排失败都不该覆盖它。
        reason = degraded_reason or outcome.degraded_reason
        degraded = reason is not None
        degraded_reason = reason
        rerank_info = outcome.rerank
    elif resolved_mode == MODE_BM25:
        chunks = await _retrieve_by_keyword(
            db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            query=optimized.search_query,
            top_k=recall_top_k,
        )
    else:  # MODE_VECTOR —— 剩下的唯一可能；未知模式已在函数开头被拒。
        chunks = await _retrieve_by_vector(
            db,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            queries=optimized.vector_queries,
            top_k=recall_top_k,
        )

    collapsed = _collapse_results(
        db,
        tenant_id=tenant_id,
        chunks=chunks,
        options=options,
    )

    empty_reason = _empty_reason(db, tenant_id=tenant_id, kb_ids=kb_ids, chunks=collapsed)
    retrieval_meta: dict[str, object] = {
        "mode": resolved_mode,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "empty_reason": empty_reason,
        # 重排事实（是否发生、候选数、返回数、耗时）。单模式下恒为 null：
        # 「重排」的定义是对**融合后**的候选重排，没有融合就没有这一步。
        "rerank": rerank_info,
    }
    if optimized.rewrite_applied:
        # 查询优化的**实际用句**（#31）：不记录它，评测里就分不清「名次变了是因为
        # 检索不行，还是因为改写把问题改坏了」——q16 的退化当年正是因此无法归因。
        #
        # 只在改写**真的发生**时才加这一栏：改写关闭 / 失败时它是恒等变换（用句就是
        # 原话），加进去只会让「关闭改写时响应逐字节不变」那条回归红线失效，而
        # 没有任何可归因的信息增加。
        retrieval_meta["query_optimization"] = {
            "rewrite_applied": True,
            "rewrite_degraded": optimized.rewrite_degraded,
            "effective_query": optimized.effective_query,
            "vector_queries": list(optimized.vector_queries),
        }

    metadata = _metadata_of(
        options,
        kb_ids=kb_ids,
        retrieval=retrieval_meta,
        trace_id=trace_id,
    )

    # 归属索引的写入是**旁路**（ADR-0021 决策 2）：独立短事务、失败只留 warning。
    # 放在这里而不是更早，是因为它要记「这次检索的结局」——降级原因与空结果原因在上一行
    # 才最终成形；而 ``trace_id`` 早在函数开头就派生好了，所以这次写入失败不影响响应。
    record_retrieval_trace(
        log_id=log_id,
        trace_id=trace_id,
        tenant_id=tenant_id,
        kb_ids=list(kb_ids),
        chunk_ids=delivered_chunk_ids(collapsed),
        top_k=resolved_top_k,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        # 两个原因都从本次响应的那一层取，不另算一遍：库里那一行与回给调用方的响应
        # 对同一件事必须只有一个答案。
        degraded_reason=degraded_reason,
        empty_reason=empty_reason,
    )

    return {
        "query": query,
        "kb_ids": list(kb_ids),
        "retrieved_chunks": collapsed,
        "metadata": metadata,
    }


def _metadata_of(
    options: RetrievalOptions,
    *,
    kb_ids: list[str],
    retrieval: dict[str, object],
    trace_id: str,
) -> dict[str, object]:
    """响应的 ``metadata``：这次实际用了什么。

    ``tenant_policy``（GLOSSARY）回答「为什么这次慢了 / 为什么没重排 / 哪些参数被裁掉了」。
    它在**每一次**经套餐门禁解析的调用上都出现，且与 ``metadata.mode`` / ``top_k`` 同源；
    只有不经门禁的内部调用（自造 ``options`` 的评测脚本）没有它——那时编一个套餐名出来
    只会更糟。

    ``trace_id`` 挂在**顶层且最后**（#101）：它是「这一次调用的身份」，不是「这一次检索的
    细节」，所以不进 ``retrieval``；放最后是为了让「把它摘掉之后与原响应逐字节相同」这件事
    在字节层面直接成立（黄金哈希就是这么比的）。
    """

    metadata: dict[str, object] = {
        "mode": options.mode,
        "top_k": options.top_k,
        "vector_store": settings.VECTOR_STORE,
        # 降级与空结果统一挂在这一层（父 spec「响应契约」）：调用方读一个地方
        # 就能判断「这是不是完整结果」「为什么是空的」。
        "retrieval": retrieval,
    }
    echo = tenant_policy_echo(options, kb_count=len(kb_ids))
    if echo is not None:
        metadata["tenant_policy"] = echo
    metadata["trace_id"] = trace_id
    return metadata


# 多取倍数住在 IO 无关的折叠模块里（评测也要用它，不该为此把 settings/DB 拖进
# 脚本）。检索侧直接引用，不再留一个只做转发的别名。


def _meta_of(chunk: dict, metas: dict[tuple[str, str], dict]) -> dict:
    """取这条结果的 meta；唯一键是 ``(kb_id, chunk_id)``（ADR-0010）。

    单独按 chunk_id 取会让跨库同 ID 的两条结果互相覆盖。
    """

    chunk_id = chunk.get("chunk_id")
    kb_id = chunk.get("kb_id")
    if not isinstance(chunk_id, str) or not isinstance(kb_id, str):
        return {}
    return metas.get((kb_id, chunk_id)) or {}


def _collapse_results(
    db: Session,
    *,
    tenant_id: str,
    chunks: list[dict],
    options: RetrievalOptions,
) -> list[dict]:
    """把命中的小片折成「交付单元」：一条结果代表一节（ADR-0013）。

    检索仍按小片算分（保住「找得准」），返回前把同一父节的命中折成一条，
    所以 ``options.top_k`` 的名额不会被同一节的碎片占满。

    三件事在这里一次做完：折叠、补定位字段（页码 / 稿号 / 切块策略）、
    拼出对外字典。折叠算法本身在 ``result_collapse`` 里——**评测调同一个函数**，
    口径不会再和生产分叉。

    形状偏好读的是同一个 ``options``：折叠与「表格行折回整表」是**这次调用的参数**，
    不是本函数的可选开关，所以它们不该再以独立参数的形式出现在这里。
    """

    if not chunks:
        return []

    if not options.fold_to_sections:
        # 不折叠：返回小片，响应与折叠之前逐字一致。留给「只要精确到那一行」的调用方。
        return _with_locating_fields(db, tenant_id=tenant_id, chunks=chunks)

    kb_ids = sorted({str(chunk["kb_id"]) for chunk in chunks if chunk.get("kb_id")})
    document_ids = sorted(
        {str(chunk["document_id"]) for chunk in chunks if chunk.get("document_id")}
    )
    if not kb_ids or not document_ids:
        return chunks

    metas = chunk_repository.fetch_meta_by_chunk_ids(
        db,
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        chunk_ids=[str(chunk["chunk_id"]) for chunk in chunks if chunk.get("chunk_id")],
    )

    hits: list[result_collapse.Hit] = []
    for chunk in chunks:
        meta = _meta_of(chunk, metas)
        hits.append(
            result_collapse.Hit(
                chunk_id=str(chunk.get("chunk_id") or ""),
                document_id=str(chunk.get("document_id") or ""),
                score=float(chunk.get("score") or 0.0),
                content=str(chunk.get("content") or ""),
                chunk_type=str(meta.get("chunk_type") or ""),
                section_id=str(meta.get("section_id") or ""),
                parent_section_id=str(meta.get("parent_section_id") or ""),
                table_part=int(meta.get("table_part") or 0),
                heading_path=str(meta.get("heading_path") or ""),
                kb_id=chunk.get("kb_id"),
            )
        )

    # 一次查出这些单元（h2 级章节）下的全部切片，含挂在该节自己身上的片。
    # 用路径前缀而不是 parent_section_id：后者是上一级，对 h2 级的切片会指到 h1，
    # 按它归组会把整篇文档并成一个单元（这个坑真踩过）。
    unit_paths = sorted(
        {
            result_collapse.unit_path_of(hit.heading_path)
            for hit in hits
            if hit.heading_path
        }
    )
    raw_rows = (
        chunk_repository.fetch_unit_rows(
            db,
            tenant_id=tenant_id,
            document_ids=document_ids,
            unit_paths=unit_paths,
            index_version=DEFAULT_INDEX_VERSION,
        )
        if unit_paths
        else {}
    )

    # 一个单元键 = (种类, 文档, 单元 id[, table_part])。单元 id 由路径算出来
    # （sha256(前两段)），所以不需要新增列，重建索引前后都稳定。
    sections: dict[result_collapse.UnitKey, result_collapse.SectionRows] = {}
    tables: dict[result_collapse.UnitKey, str] = {}
    for (document_id, path), rows in raw_rows.items():
        unit_id = result_collapse.unit_id_of(path)
        if not unit_id:
            continue
        sections[result_collapse.UnitKey(result_collapse.UNIT_SECTION, document_id, unit_id)] = tuple(
            (index, content, chunk_id) for index, content, chunk_id, _type, _part in rows
        )
        for _index, content, _chunk_id, chunk_type, table_part in rows:
            if chunk_type == "table":
                tables[
                    result_collapse.UnitKey(
                        result_collapse.UNIT_TABLE, document_id, unit_id, str(table_part)
                    )
                ] = content

    units = result_collapse.collapse(
        hits,
        sections=sections,
        tables=tables if options.expand_table_rows else None,
        top_k=options.top_k,
        merge_table_rows=options.expand_table_rows,
    )

    # 唯一键是 (kb_id, chunk_id)（ADR-0010）：跨库同 ID 的两条结果必须能各自取到
    # 自己那条，否则代表片会拿错库的分数字段，甚至拿到空 dict。
    by_result = {
        (str(chunk.get("kb_id")), str(chunk.get("chunk_id"))): chunk for chunk in chunks
    }
    results: list[dict] = []
    for unit in units:
        # 以代表片那条结果为底：向量分 / 融合分 / 名次这些对外字段要原样保留，
        # 只把「身份、正文、分数、溯源」换成单元的。
        row = dict(by_result.get((str(unit.kb_id), unit.chunk_id)) or {})
        row["chunk_id"] = unit.chunk_id
        row["content"] = unit.content
        row["score"] = unit.score
        row["section_id"] = unit.section_id or None
        row["heading_path"] = unit.heading_path or None
        row["source_chunk_ids"] = list(unit.source_chunk_ids)
        row["expansion"] = unit.expansion
        results.append(row)

    # meta 已经在上面查过一次，直接复用：同一次检索不为同一批 ID 查两遍库。
    return _with_locating_fields(db, tenant_id=tenant_id, chunks=results, metas=metas)


def _with_locating_fields(
    db: Session,
    *,
    tenant_id: str,
    chunks: list[dict],
    metas: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """补页码 / 稿号 / 切块策略：引用要能说清「哪一页、哪一稿、怎么切的」。

    ``metas`` 可以由调用方传进来复用——折叠路径上已经查过一次，不该为同一批 ID
    再查一遍库。
    """

    if metas is None:
        ids = [str(chunk["chunk_id"]) for chunk in chunks if chunk.get("chunk_id")]
        kb_ids = sorted({str(chunk["kb_id"]) for chunk in chunks if chunk.get("kb_id")})
        if not ids or not kb_ids:
            return chunks
        metas = chunk_repository.fetch_meta_by_chunk_ids(
            db, tenant_id=tenant_id, kb_ids=kb_ids, chunk_ids=ids
        )
    for chunk in chunks:
        meta = _meta_of(chunk, metas)
        page = meta.get("page")
        doc_version = meta.get("doc_version")
        strategy = meta.get("split_strategy")
        chunk["page"] = page if isinstance(page, int) else None
        chunk["doc_version"] = doc_version if isinstance(doc_version, int) else None
        chunk["split_strategy"] = strategy if isinstance(strategy, str) else None
    return chunks


def _rewrite_hint(kbs: list) -> str | None:
    """把本次涉及的库的「名称（描述）」拼成一句，给改写提示词当上下文。

    这是**可选**的：拿不到名称 / 描述时改写照样能做，只是提示词更短。单个库时它就是
    「这个库是讲什么的」；多库检索时它是一句罗列——改写的目标是「用户这句话该怎么说
    文档里的话」，知道在查什么库显然更准。

    全部为空时返回 ``None`` 而不是空串：空串会被 ``if hint`` 判成「没有 hint」，
    但拼进提示词的那一侧多一个空行总是噪音。
    """

    parts: list[str] = []
    for kb in kbs:
        name = (getattr(kb, "name", None) or "").strip()
        description = (getattr(kb, "description", None) or "").strip()
        if name and description:
            parts.append(f"{name}（{description}）")
        elif name or description:
            parts.append(name or description)
    return "；".join(parts) or None


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
    db: Session, *, tenant_id: str, kb_ids: list[str], queries: Sequence[str], top_k: int
) -> list[dict]:
    """向量通道：逐句一次范围召回覆盖全部配对，**不按库循环、不按库分名额**。

    多句时先并成一个池（``vector_recall`` 里去重取优）；单句（改写关闭 / 失败）时
    走原来的快路径，SQL 语义、排序口径与打分全部与票 4 一致（回归红线）。
    """

    rows = await vector_recall(
        db=db,
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        queries=queries,
        top_k=top_k,
        embedding_factory=get_embedding_provider,
        vector_factory=get_vector_store,
    )

    # 单查询时这一步是稳定排序（``vector_recall`` 已按同一口径排过），逐字节不变。
    rows.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return [
        # 五列契约字段统一由 ``app.utils.retrieval_fields`` 补齐：单模式下只有本通道
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
