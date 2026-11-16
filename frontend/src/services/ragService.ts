import { dataHttp } from '../lib/dataHttp'
import type { ResearchData } from '../types/research'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'

/**
 * 数据面调用的**唯一出口**：四条检索能力端点都在这里发，别的文件不再直接 `dataHttp.post`。
 *
 * 登录 / 我的 / 树 / 建库 / 上传 / 重建 / 删文档 / Key 与配置全部走控制面会话门；
 * 只有「拿真实 Key 打真实检索」这一件事必须走 Bearer（issue #92 的边界）。
 *
 * 请求体**只有** `kb_ids` + `query`（ADR-0010）：加工方式由端点表达，质量参数由 Key
 * 的密钥配置表达，页面不拼任何质量字段。后端 `extra=forbid`，所以显式逐个字段组包，
 * 不把页面状态对象整体透传。
 */
function postRetrieve(path: string, payload: RetrieveRequest): Promise<RetrieveData> {
  return dataHttp.post<RetrieveData>(path, {
    kb_ids: payload.kb_ids,
    query: payload.query,
  })
}

/** 原句直查：零模型调用、结果可复现。 */
export function retrieve(payload: RetrieveRequest): Promise<RetrieveData> {
  return postRetrieve('/api/v1/rag/retrieve', payload)
}

/** 改写后检索：一次改写模型调用，原句与改写句并池取优。 */
export function rewriteRetrieve(payload: RetrieveRequest): Promise<RetrieveData> {
  return postRetrieve('/api/v1/rag/rewrite', payload)
}

/** 问题分解后检索：拆成子问题各查一次，合并成一份列表。 */
export function decomposeRetrieve(payload: RetrieveRequest): Promise<RetrieveData> {
  return postRetrieve('/api/v1/rag/decompose', payload)
}

/** 受控 Research：多轮补证 + 引用块（响应形状不同，单独的类型）。 */
export function research(payload: RetrieveRequest): Promise<ResearchData> {
  return dataHttp.post<ResearchData>('/api/v1/rag/research', {
    kb_ids: payload.kb_ids,
    query: payload.query,
  })
}
