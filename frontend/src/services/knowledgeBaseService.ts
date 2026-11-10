import { dataHttp } from '../lib/dataHttp'
import type {
  CreatedKnowledgeBase,
  CreateKnowledgeBaseRequest,
} from '../types/knowledgeBase'

/**
 * 创建知识库。
 *
 * 路径不是 REST 的 `POST /knowledge-bases`，而是事实底座里写明的
 * `POST /knowledge-bases/create`；用错路径会撞到 404 而不是业务码。
 */
export function createKnowledgeBase(
  payload: CreateKnowledgeBaseRequest,
): Promise<CreatedKnowledgeBase> {
  return dataHttp.post<CreatedKnowledgeBase>('/api/v1/knowledge-bases/create', payload)
}
