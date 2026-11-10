import { dataHttp } from '../lib/dataHttp'
import type {
  CreatedKnowledgeBase,
  CreateKnowledgeBaseRequest,
  KnowledgeBaseListData,
  KnowledgeBaseNode,
  KnowledgeBaseSummary,
} from '../types/knowledgeBase'
import { fetchDocuments } from './documentService'

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

/** 列表页树的 query key。#89 把接口换成控制面一次请求时，缓存键不变，页面无需改。 */
export const KNOWLEDGE_BASE_TREE_QUERY_KEY = ['knowledge-bases', 'tree'] as const

/**
 * 当前 Key 可见的知识库列表。
 *
 * 后端已按 Key 的 `allowed_kb_ids` 白名单收窄；前端不做第二层过滤，
 * 否则会与后端规则慢慢漂移（多一个库显示不出来时根本分不清是哪一层的锅）。
 */
export function fetchKnowledgeBases(): Promise<KnowledgeBaseSummary[]> {
  return dataHttp
    .get<KnowledgeBaseListData>('/api/v1/knowledge-bases')
    .then((data) => data.knowledge_bases)
}

/**
 * 列表页两级树（阶段 1 实现）。
 *
 * **对外只暴露 `Promise<KnowledgeBaseNode[]>`**：页面与轮询不许知道它是
 * 「1 次库列表 + 每库 1 次文档列表」拼出来的。T7/#89 会把它换成控制面
 * `/console/knowledge-bases/tree` 的一次请求，那时只改本函数的实现，
 * 页面、`useTreePolling`、`StatusBadge` 一行都不用动。
 *
 * 各库的文档请求并发发出而不是串行：结果只是拼树，库之间没有先后依赖，
 * 串行会让「N 个库」把列表页首屏拉成 N 倍的往返延迟。
 */
export async function fetchTree(): Promise<KnowledgeBaseNode[]> {
  const knowledgeBases = await fetchKnowledgeBases()
  const documentsByKb = await Promise.all(
    knowledgeBases.map(
      async (kb) => [kb.kb_id, await fetchDocuments(kb.kb_id)] as const,
    ),
  )
  const documents = new Map(documentsByKb)

  return knowledgeBases.map((kb) => ({
    kb_id: kb.kb_id,
    name: kb.name,
    split_strategy: kb.split_strategy,
    allowed_formats: kb.allowed_formats,
    chunk_size: kb.chunk_size,
    documents: documents.get(kb.kb_id) ?? [],
  }))
}
