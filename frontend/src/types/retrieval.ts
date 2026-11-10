/**
 * 检索接口的类型：字段以 `step8/00-facts.md` §5 为准，不自己发明。
 *
 * `metadata` 在后端是 `dict`，这里把**本页实际会读**的那些字段写实：运行摘要必须
 * 只从响应读，所以每个标签在类型上都能对应到一条真实路径（不存在的字段别再引用：
 * 00-facts 明确列过 `latency_ms` / `log_id` / `trace_id` / 各计数 / `keyword_search`
 * 都不存在）。
 *
 * 「未命中的那一路是 null 而不是 0」是本文件所有可空数值字段的共同语义：
 * 渲染时用 `formatScore` / `formatPercent` / `formatRank` 折成「—」，不能 `?? 0`。
 */

/** `POST /rag/retrieve` 的请求体。后端 `extra=forbid`：**只有这两个字段**。 */
export interface RetrieveRequest {
  kb_ids: string[]
  query: string
}

/** 一条召回结果（折叠后一条 = 一节，`chunk_id` 是让这一节浮上来的代表片）。 */
export interface RetrievedChunk {
  document_id: string
  chunk_id: string
  title: string
  content: string
  /** 本次最终排序分（折叠开启时是单元内命中片的最高分，见 ADR-0010）。 */
  score: number
  /** 跨库检索时才知道这条来自哪个库。 */
  kb_id: string | null
  vector_score: number | null
  bm25_score: number | null
  vector_rank: number | null
  bm25_rank: number | null
  rrf_score: number | null
  /** 只在真的发生重排时出现，与 `score` 同值。 */
  rerank_score: number | null
  section_id: string | null
  source_chunk_ids: string[]
  /** 这次上下文给了多大范围：neighbors / parent / table / chunk（块类型）。 */
  expansion: string | null
  heading_path: string | null
  page: number | null
  doc_version: number | null
  split_strategy: string | null
}

/** `metadata.retrieval.rerank`：重排事实；单模式下恒为 null。 */
export interface RerankInfo {
  applied?: boolean
  candidates?: number
  returned?: number
  duration_ms?: number
}

/** `metadata.retrieval`：降级与空结果都挂在这一层，读一个地方就够。 */
export interface RetrievalBlock {
  /** 后端这里给的是**本次实际解析出**的检索模式。 */
  mode: string
  degraded: boolean
  /** 四个英文码之一（照抄原文，不做中文替身），不降级时为 null。 */
  degraded_reason: string | null
  /** `no_indexed_chunks` / `no_chunks_matched`，有结果时为 null。 */
  empty_reason: string | null
  rerank: RerankInfo | null
  /**
   * 只有改写真的发生时后端才加这一栏；本票不展示，留成可选是为了不把它当成
   * 契约的一部分（字段缺失不等于响应坏了）。
   */
  query_optimization?: unknown
}

/** `metadata.tenant_policy`：这次调用**实际生效**了什么（GLOSSARY）。 */
export interface TenantPolicy {
  plan: string
  requested_profile: string
  mode: string
  top_k: number
  rerank: boolean
  rewrite: boolean
  kb_count: number
  /** 被套餐上限裁掉的字段名；空数组表示没裁。 */
  clipped: string[]
}

/** 响应的 `metadata`。 */
export interface RetrievalMetadata {
  mode: string
  top_k: number
  vector_store: string
  retrieval: RetrievalBlock
  /**
   * 只有经套餐门禁解析的调用才有这一栏；内部 / 评测调用没有套餐可回显。
   * 摘要里「若有才显示」的就是它。
   */
  tenant_policy?: TenantPolicy
}

/** `POST /rag/retrieve` 的响应 data。`kb_ids` 是这次**实际**查询的库（服务端回显）。 */
export interface RetrieveData {
  query: string
  kb_ids: string[]
  retrieved_chunks: RetrievedChunk[]
  metadata: RetrievalMetadata
}

/**
 * 本页可勾选的库。字段与 `GET /knowledge-bases` 的单项一致（00-facts §4）。
 *
 * 没复用 `types/knowledgeBase.ts` 里的建库类型：那份由 #86 并行改动（它会加列表 /
 * 树相关类型），这里只需要「勾选用的四个字段 + kb_id」，自己声明一份更不容易冲突，
 * 也不会让本页跟着那一边的类型变更一起漂。
 */
export interface PickableKnowledgeBase {
  kb_id: string
  name: string
  tenant_id: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
}

/** `GET /knowledge-bases` 的响应 data。 */
export interface KnowledgeBaseListData {
  knowledge_bases: PickableKnowledgeBase[]
}
