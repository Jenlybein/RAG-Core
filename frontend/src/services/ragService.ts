import { dataHttp } from '../lib/dataHttp'
import type {
  KnowledgeBaseListData,
  PickableKnowledgeBase,
  RetrieveData,
  RetrieveRequest,
} from '../types/retrieval'

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
 * 用数据面的 `GET /knowledge-bases`：它已经按这把 Key 的密钥配置白名单收窄过，
 * 所以「列出来的库」就是「这次检索能查的库」，不需要页面再自己过滤一遍。
 * 阶段 2 换成控制面的 tree 接口时只换这个函数的实现，页面与勾选状态都不用动。
 */
export async function fetchPickableKnowledgeBases(): Promise<PickableKnowledgeBase[]> {
  const data = await dataHttp.get<KnowledgeBaseListData>('/api/v1/knowledge-bases')
  // 服务返回 `{knowledge_bases: [...]}`；页面只关心数组，壳在这一层剥掉。
  return data.knowledge_bases
}
