"""检索业务：一次检索的**外层**——组合根 + 那五件「每次调用只做一次」的事（#140）。

边界：不拼上下文、不生成答案，只返回切片 + 分数 + 名次 + 出处。``tenant_id`` 来自密钥
解析（数据面的身份只有一份判定，见 ``app.core.auth``），``kb_ids`` 是调用方显式传的列表。
``kb_ids ⊆ allowed_kb_ids`` 的**白名单关卡不在这里**：它是六道关卡的第二道，由两个入口
（HTTP 路由与 MCP 工具）在进本函数之前调 ``app.services.data_scope.ensure_kb_scope`` 判掉；
本模块只负责「租户对这几个库可不可见」（``ensure_visible``）与检索链路本身。

**检索本身在 ``app/services/retrieval/kernel.py``**（ADR-0028）。本函数是它的调用方，
把「一次检索」交给内核，自己做外层那五件事——它们的共同点是**每次调用只做一次**，
而内核是「每条子查询各做一次」：

| 留外层的 | 为什么不能进内核 |
| --- | --- |
| 查询优化（改写 / 同义词） | 内核被调用 N 次，改写就是 N 次；Research 里再改一遍会把已分解的子查询揉回原问题 |
| 可见性预校验 | 范围是**这次调用**的属性，不是每次子检索的属性；重复校验只是把判断乘以 N |
| 配额与记账 | 内核不知道自己被谁调用；按子检索记就是扣 N 次 |
| 归属索引写入 | 一次调用一行，``chunk_ids`` 存最终证据的节 |
| 观测提交 | 阶段预算按整次调用算；N 份 trace 让成本与延迟无从归因 |

再加上**空结果原因计算**与 **``metadata`` 组装**：前者的答案属于「这次调用遇到了什么」，
后者是响应契约（ADR-0010）而不是检索步骤。

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
  三条查询串**原样交给内核**（它不再加工一遍）。
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
  故障与重排失败都不覆盖它。内核交回它自己那几段的原因，本条合成在这里做。

**三种结局**（父 spec「降级矩阵」）：拒绝（业务错误）/ 降级（成功 + 标记）/
诚实空（成功 + ``empty_reason``）。空结果不是故障：把它当故障处理会让监控
满地假警报，也让调用方误以为该重试。

**质量参数从哪来（第 7 步，#77 / #78）**：请求体只有 ``kb_ids`` + ``query``；模式、返回
条数、重排、改写、折叠与表格展开由平台层从密钥配置解析、并过完五道门禁
（``app.services.profile_policy``）后交成一个 :class:`RetrievalOptions` 进来。本模块因此
**不再认识请求模型**，也不再为任何一个参数写「没传就取缺省」的分支；两个布尔开关
（``options.rerank`` / ``options.rewrite``）同理——它们是**生效值**（档位意图 ∧ 本机
能力），本模块不再自己去问一次 ``settings``。

``recall_depth`` / ``rerank_candidates`` 这两个**服务端旋钮**仍在这里读
（``HYBRID_RECALL_TOP_K`` / ``RERANK_MAX_CANDIDATES``）：它们是「这台机器怎么部署」，
不是「这次调用要什么」。读出来之后**当参数交给内核**——内核不碰 ``settings``，
范围与深度因此只有一个来源。

**每次响应都带 ``tenant_policy``**：套餐名、要求的档位、实际模式、条数、重排、改写、
本次用到的库数、被裁字段。它从**同一个** ``options`` 上读出（``tenant_policy_echo``），
所以「回显说开了重排」与「链路真的开了重排」不可能分叉——这份回显不是另存的一份事实。

**每次响应都带 ``trace_id``，而且它先于写库产生**（#101，ADR-0010「第五次修订」）：
``log_id`` 先生成（UUID v4），``trace_id`` 立刻由它确定性派生（``trace_identity``），
然后才在最后尝试往归属索引里写一行结构记录。顺序不能倒：写入是旁路，写失败只留 warning，
响应里的 ``trace_id`` 照样有值——「写成功才拿到 id」等于把契约挂在旁路的健康度上。

除 ``metadata.trace_id`` 之外，检索响应逐字节不变；六段耗时**不进**响应（那是观测的事）。

**六段耗时从这里开始、也在这里交出去**（#102）：本函数创建一个
:class:`app.observability.stage_timing.StageTimer`，自己记查询优化那一段，剩下的
**显式传给内核**（它在召回 / 融合 / 重排 / 折叠四处记），最后把收集到的段交给当前去向
（``app.observability.emit_retrieval_trace``）。三条性质因此成立：

* **度量常开**：收集器**无条件**创建，与 ``TRACE_SINK`` 无关——关掉观测不等于关掉计时；
* **段名与顺序不变**：段名只有 ``app/observability/stage_timing.py`` 一份，谁记哪一段
  是显式传参的结果，不是两个模块各写一遍字符串；
* **没有全局可变状态**：一次检索一份收集器，两次并发请求的六段不会串到一个列表里
  （这也是它能被测试注入、并直接断言「拿到了哪几段」的原因）。

日志只放 ID 与派生量，绝不放检索原文（见 tests/test_retrieval_log_hygiene.py）。

**整条链路在异步栈上**（第 11 步票 #127）：``db`` 是请求级 ``AsyncSession``
（``app.db.async_session.get_async_db``），可见性校验、向量召回（pgvector）、折叠回查、
空结果计数与归属索引写入全部 ``await``。第 10 步的「固定规模检索执行池 + 线程包装」因此
被删除——它存在的唯一理由（同步 pgvector 查询会卡住事件循环）已经不存在了。响应契约
一个字节都没动：三种模式的黄金哈希仍然绿（``tests/test_multi_query_union.py``）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping

import structlog

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.models.index_version import resolve_index_versions
from app.observability import emit_retrieval_trace_off_loop
from app.observability.stage_timing import STAGE_QUERY_OPTIMIZATION, StageTimer
from app.providers.embedding import get_embedding_provider
from app.providers.keyword_search import get_keyword_search_provider
from app.providers.vectorstores import get_vector_store
from app.repositories import chunk_repository
from app.services import orchestration, result_collapse
from app.services.knowledge_base_access import ensure_visible
from app.services.query.optimizer import optimize_query
from app.services.query.synonyms import collect_synonym_groups
from app.services.retrieval.kernel import retrieve_kernel
from app.services.retrieval_options import (
    MODE_BM25,
    MODE_HYBRID,
    MODE_VECTOR,
    RetrievalOptions,
    tenant_policy_echo,
)
from app.services.retrieval_trace import record_retrieval_trace
from app.services.trace_identity import (
    delivered_chunk_ids,
    tenant_fingerprint,
    trace_id_for,
)
from app.utils.id_generator import new_retrieval_log_id

logger = structlog.get_logger(__name__)

# 空结果的两种原因：调用方据此决定「去传文档」还是「换个问法」。
EMPTY_NO_INDEXED_CHUNKS = "no_indexed_chunks"
EMPTY_NO_CHUNKS_MATCHED = "no_chunks_matched"


async def retrieve(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    options: RetrievalOptions,
    stage_timer: StageTimer | None = None,
    rerank_factory: Callable[[], Awaitable[object]] | None = None,
) -> dict:
    """按 ``options.mode`` 走一条通道（或并行两路）。

    ``options`` 是**已经解析完**的本次参数（模式、返回条数、重排、改写、两个结果形状
    开关），由平台层过完门禁后交进来（``app.services.profile_policy``）——请求体里只剩
    ``kb_ids`` + ``query``，质量参数没有第二个来源。

    本函数是**组合根**：它读那些**不是每次调用都能选**的服务端旋钮（召回深度
    ``HYBRID_RECALL_TOP_K``、重排候选池 ``RERANK_MAX_CANDIDATES``），把它们与
    「已解析好的上下文」一起交给内核（``app.services.retrieval.kernel``）。它继续校验
    模式——MCP 与内部调用仍可能自己造一个 ``options``，模式词表那一道不能因为有了值对象
    就省掉，而且它必须在可见性校验与改写之前发生（被拒的请求不该白烧一次改写）。

    **重排与改写的开关读 ``options``，不读 ``settings``**：这两个布尔已经是「档位想要 ∧
    本机装了」的合取（``app.services.retrieval_options.options_from_chain``）。链路里
    再问一次 ``settings`` 就等于有了两个真相——机器上没装重排、档位却想要，两处判断
    谁说了算会变成排障时的第一道谜题。

    ``stage_timer`` 是**可选的**收集器（#102）：不传就自己造一个，六段照样计时，只是
    这份计量没有别人收下（去向是 ``none`` 时正是如此）。测试传一个进来，就能直接断言
    「这一次到底记到了哪几段」——「度量常开」因此是可观察的事实，而不是注释里的承诺。

    ``rerank_factory`` 是重排的**注入缝**（#116）：缺省 ``None`` 时内核走既有工厂出口
    （``get_rerank_provider``），传一个别的工厂进来就换实现——评测靠它显式装配旧的大模型
    重排做对照，生产路径一行不用改。它是**协程工厂**（票 #137）：旧实现要 ``await`` 一次
    Provider 装配，缺省那一条也被包成协程，两层形状一致。它只影响**混合模式**：重排的
    定义就是对融合后的候选重排，单模式没有这一步。本函数只负责原样往下传，一步都不改
    ——「收下就丢」会让这条接缝变成一句没人验证的承诺。
    """

    # 度量常开：收集器无条件创建，与 ``TRACE_SINK`` 无关。去向只决定这份计量送到哪。
    timer = stage_timer if stage_timer is not None else StageTimer()

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
    visible_kbs = [
        await ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id) for kb_id in kb_ids
    ]

    # 查询优化：召回之前把用户那句话加工成「向量逐句列表 + 关键词一句 + 重排一句」
    # （GLOSSARY「检索链路」：查询优化 → 召回 → 融合 → 重排）。改写开关读 ``options``：
    # 它是「档位想要 ∧ 本机装了」的合取，关闭时这一步是恒等变换。
    with timer.measure(STAGE_QUERY_OPTIMIZATION):
        optimized = await optimize_query(
            query=query,
            # 词表是**库级配置**：只合并本次涉及的库，不跨库、不跨租户。
            synonym_groups=collect_synonym_groups(
                [getattr(kb, "settings", None) for kb in visible_kbs]
            ),
            rewrite_hint=_rewrite_hint(visible_kbs),
            rewrite_enabled=options.rewrite,
        )

    # 范围由**这一次调用**解析一次：``kb_id → 索引版本`` 的映射（不是两个平行数组，
    # 配对关系只能从映射里读出来）。内核与将来的 Research 都吃这一份，不各自去造。
    #
    # 版本**从上面那几行库行里读**（ADR-0025 决策四）：不再是一个常量。这是「这次查的是
    # 哪一次构建」的唯一来源，也是本步相对上一版唯一的取值变化——范围条件的形状一个字
    # 没改。某个库还没有生效版本（回填漏做 / 还没激活过任何构建）时它会抛
    # ``IndexVersionUnresolved``：**在召回之前**拦下，而不是拼出一条恒不匹配的条件、
    # 交回一份「看起来像没命中」的空结果。
    index_versions = resolve_index_versions(visible_kbs)

    outcome = await retrieve_kernel(
        db,
        tenant_id=tenant_id,
        index_versions=index_versions,
        query=query,
        mode=resolved_mode,
        # 内核拿三条查询串：向量一路逐句都搜（改写成功时原句 + 改写句），重排看
        # effective_query，关键词一路拿扩展后的句子（#31）。
        vector_queries=optimized.vector_queries,
        rerank_query=optimized.effective_query,
        keyword_query=optimized.search_query,
        top_k=resolved_top_k,
        recall_top_k=recall_top_k,
        # 「召回宽、返回窄、融合池更宽」：两个深度只在这里读配置，内核不碰 settings。
        # 融合池宽度只在**这次要重排**时才加宽——不重排时让它等于 top_k，与加宽之前
        # 逐字节一致（候选多了也不会有下次重排去挑）。
        recall_depth=max(resolved_top_k, settings.HYBRID_RECALL_TOP_K),
        rerank_enabled=options.rerank,
        rerank_candidates=settings.RERANK_MAX_CANDIDATES if options.rerank else None,
        rerank_factory=rerank_factory,
        # 形状偏好读同一个 ``options``：它们是**这次调用的参数**，不是内核的可选开关。
        fold_to_sections=options.fold_to_sections,
        expand_table_rows=options.expand_table_rows,
        # 收集器显式传进去：内核在召回 / 融合 / 重排 / 折叠四处记，段名与上面的常量同源。
        stage_timer=timer,
        # 工厂以对象传入：测试替换工厂的位置因此仍只有一处（既有 monkeypatch 接缝）。
        # 三个名字在**本模块**的命名空间里解析，所以 ``monkeypatch.setattr(service, ...)``
        # 这条接缝一个字不用改。
        embedding_factory=get_embedding_provider,
        vector_factory=get_vector_store,
        keyword_factory=get_keyword_search_provider,
    )

    collapsed = outcome.chunks
    # 一因一码，先到的才是根因：改写发生在召回之前，所以它已经在场时通道故障与
    # 重排失败都不该覆盖它。
    degraded_reason = optimized.rewrite_reason or outcome.degraded_reason

    # ── 切片编排（第 11 步票 #147）：**折叠之后**做，此时交付单元已定 ──
    # 位置就是这个函数里唯一说得通的那一个：编排的输入是「交付单元」，而交付单元在折叠
    # 之后才存在（之前是命中片，一条结果可能对应同一节的多片）。放在折叠之前等于让模型
    # 在同一节的碎片之间挑，挑出来的「核心片」与调用方看到的材料对不上。
    #
    # 开关读 ``options.orchestration``（生效值 = 档位想要 ∧ 配置启用，套餐越权在门禁里
    # 已经拒绝）。关闭时这一段**整段不执行**：不调模型、不读配置、响应里也不出现那个键
    # ——「附加能力的代价只付给开了它的档位」是可观察的事实，不是一句承诺。
    orchestration_block = None
    if options.orchestration and collapsed:
        orchestrated = await orchestration.orchestrate_evidence(
            query=query,
            rows=collapsed,
            tenant_id=tenant_id,
            # 与召回、折叠、归属索引**同一份**范围映射：编排材料不许来自别的库或别的版本。
            index_versions=index_versions,
        )
        orchestration_block = orchestrated.block
        # 编排在最后一步，所以它**永不覆盖**前面任何一条原因（先到的才是根因）。
        degraded_reason = degraded_reason or orchestrated.degraded_reason
    degraded = degraded_reason is not None

    # 空结果判定用**同一个范围**（``kb_id → 生效版本`` 的映射）：换一个版本口径去数，
    # 就会把「这个库有内容、只是本次没命中」说成「这个库是空的」。
    empty_reason = await _empty_reason(
        db, tenant_id=tenant_id, index_versions=index_versions, chunks=collapsed
    )
    retrieval_meta: dict[str, object] = {
        "mode": resolved_mode,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "empty_reason": empty_reason,
        # 重排事实（是否发生、候选数、返回数、耗时）。单模式下恒为 null：
        # 「重排」的定义是对**融合后**的候选重排，没有融合就没有这一步。
        "rerank": outcome.rerank,
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

    # 观测旁路（#102）：六段耗时交出去。放在这里是因为「这次是个什么结局」刚刚成形；
    # 与下面那次写库一样，它**不影响返回值**——收集器早已计完时，去向那边连不上也只留
    # 一条 warning（``app.observability.emit_retrieval_trace``）。属性只有结构化事实：
    # 模式、条数、降级与空结果原因。
    #
    # ``query`` 单独一个参数（#110 复盘后补）：它是**原文通道**，收不收由去向决定——
    # 观测栈收（低分样本导出要靠它），本地文件丢。放进 attributes 会让「结构化事实」
    # 与原文混成一份，也让 jsonl 那一路无法把它们分开。
    await emit_retrieval_trace_off_loop(
        trace_id=trace_id,
        spans=timer.spans(),
        query=query,
        attributes={
            "mode": resolved_mode,
            "top_k": resolved_top_k,
            "degraded_reason": degraded_reason,
            "empty_reason": empty_reason,
            # 租户**指纹**（不是明文）：管理端要按租户看趋势，而观测数据常常要导出给别人，
            # 明文租户标识会让「这份数据能发给谁」多一道合规问题。控制台侧可反查。
            "tenant": tenant_fingerprint(tenant_id),
        },
    )

    # 归属索引的写入是**旁路**（ADR-0021 决策 2）：独立短事务、失败只留 warning。
    # 放在这里而不是更早，是因为它要记「这次检索的结局」——降级原因与空结果原因在上一行
    # 才最终成形；而 ``trace_id`` 早在函数开头就派生好了，所以这次写入失败不影响响应。
    await record_retrieval_trace(
        log_id=log_id,
        trace_id=trace_id,
        tenant_id=tenant_id,
        kb_ids=list(kb_ids),
        # 「这次查的是哪次构建」（ADR-0025 决策六）：与本次范围**同一份映射**，
        # 不是另算一遍。版本一旦会变，同一个 ``kb_ids + query`` 两次调用的差异
        # 就只剩这一列能归因——切法变了？文档变了？还是召回抖了？
        index_versions=dict(index_versions),
        chunk_ids=delivered_chunk_ids(collapsed),
        top_k=resolved_top_k,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        # 两个原因都从本次响应的那一层取，不另算一遍：库里那一行与回给调用方的响应
        # 对同一件事必须只有一个答案。
        degraded_reason=degraded_reason,
        empty_reason=empty_reason,
    )

    payload: dict[str, object] = {
        "query": query,
        "kb_ids": list(kb_ids),
        "retrieved_chunks": collapsed,
        "metadata": metadata,
    }
    if orchestration_block is not None:
        # 只在开启时出现；关闭时这个键**连存在都不存在**（响应逐字节不变，见
        # ``RetrieveData`` 的序列化规则与 ADR-0010「第七次修订」）。
        payload["orchestration"] = orchestration_block
    return payload


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


async def _empty_reason(
    db: AsyncSession,
    *,
    tenant_id: str,
    index_versions: Mapping[str, str],
    chunks: list[dict],
) -> str | None:
    """空结果的原因：查一次 count，绝不按结果条数猜。

    * ``no_indexed_chunks``：这些库里一条可检索切片都没有（该去传文档）；
    * ``no_chunks_matched``：库里有内容，但本次没有命中（该换个问法）。

    有结果时返回 ``None``——``empty_reason`` 只在真为空时出现，不是一个常驻字段。

    计数与召回**同一条范围条件**（``kb_id → 生效版本`` 的配对映射）：库里可能有别的
    索引版本的切片，但它们对这次检索不可见。用一个版本常量去数会让「本该叫用户换问法」
    的库被误判成「空库」，而那正是这个字段唯一要说清的事。
    """

    if chunks:
        return None
    indexed = await chunk_repository.count_by_kb_ids(
        db, tenant_id=tenant_id, index_versions=index_versions
    )
    return EMPTY_NO_INDEXED_CHUNKS if indexed == 0 else EMPTY_NO_CHUNKS_MATCHED
