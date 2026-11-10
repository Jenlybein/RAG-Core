import { dataHttp } from '../lib/dataHttp'
// 同一个数据面接口（GET /knowledge-bases）#86 已经有唯一实现；清单怎么解析（信封、
// 白名单收窄）在那一处说得清楚，这里不再写第二份。
import { fetchKnowledgeBases } from './knowledgeBaseService'
import type { KnowledgeBaseSummary } from '../types/knowledgeBase'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'

/**
 * 发一次检索。
 *
 * 请求体**只有** `kb_ids` + `query`：检索方式、条数、重排、改写全部来自这把 Key 的
 * 密钥配置（ADR-0010「第三次修订」），页面不拼任何质量字段。后端 `extra=forbid`，
 * 多带一个就会被 20002 拒掉——所以这里显式逐个字段组包，而不是把页面状态对象整体透传：
 * 以后谁在入参上多加一个字段，也不会顺着引用悄悄进请求体。
 */
export function retrieve(payload: RetrieveRequest): Promise<RetrieveData> {
  return dataHttp.post<RetrieveData>('/api/v1/rag/retrieve', {
    kb_ids: payload.kb_ids,
    query: payload.query,
  })
}

/**
 * 本页可勾选的库清单（阶段 1）。
 *
 * 用数据面的 `GET /knowledge-bases`：后端已按这把 Key 的密钥配置白名单收窄，
 * 所以「列出来的库」就是「这次检索能查的库」，页面不再自己过滤一遍。
 * 函数名是本页的语义（勾选清单），实现直接复用列表页那一个——两个函数都打同一个
 * 接口、解析同一份信封时，第二份实现迟早会与第一份漂移。
 */
export function fetchPickableKnowledgeBases(): Promise<KnowledgeBaseSummary[]> {
  return fetchKnowledgeBases()
}
