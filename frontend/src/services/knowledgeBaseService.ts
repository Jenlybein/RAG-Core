import { dataHttp } from '../lib/dataHttp'
import type {
  CreatedKnowledgeBase,
  CreateKnowledgeBaseRequest,
  KnowledgeBaseListData,
  KnowledgeBaseNode,
  KnowledgeBaseSummary,
} from '../types/knowledgeBase'
import { fetchConsoleTree, toKnowledgeBaseNodes } from './consoleKb'

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

/** 列表页树的 query key。换实现时缓存键刻意不变：#89 只动「怎么拿」，缓存行为与页面无需改。 */
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
 * 列表页两级树（阶段 2：控制面一次拿全，见 #89）。
 *
 * 签名**刻意保持** `Promise<KnowledgeBaseNode[]>`：页面、轮询、
 * `KNOWLEDGE_BASE_TREE_QUERY_KEY` 都只认这个类型与这个函数，所以从阶段 1
 * （1 次列库 + 每库 1 次列文档）切到一次请求时，`KnowledgeFileTreePage.vue` 与
 * `useTreePolling.ts` 一行都不用改——变的是「怎么拿」而不是「拿到什么」。
 *
 * 跨门说明：本文件其余函数仍走数据面 `dataHttp`；只有这条树查询走控制面，
 * 而控制面入口与映射函数集中在 `consoleKb.ts`，这样「哪扇门」在文件粒度可辨。
 * 页面为什么能安全地用会话身份读树：路由守卫（#85）已让列表页只在登录控制面后
 * 才可达，未登录会被跳 `/login`；「Key 页 / 身份切换」是 #92 的事，本票不碰。
 */
export async function fetchTree(): Promise<KnowledgeBaseNode[]> {
  return toKnowledgeBaseNodes(await fetchConsoleTree())
}
