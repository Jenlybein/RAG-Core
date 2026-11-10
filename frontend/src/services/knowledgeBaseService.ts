import type { KnowledgeBaseNode } from '../types/knowledgeBase'
import { fetchConsoleTree, toKnowledgeBaseNodes } from './consoleKb'

/**
 * 知识库读接口的**页面级**入口。
 *
 * 这个文件里已经没有数据面（Bearer Key）的调用了：#92 把建库 / 上传 / 删文档 / 重建
 * 整体切到控制面，这些函数都搬进了 `consoleKb.ts`。只剩 `fetchTree` 与它的缓存键——
 * 页面、轮询只认这两个符号，所以「怎么拿」变了它们一行不用改。
 */

/** 列表页树查询的 query key。换实现时缓存键刻意不变（#89）：只动「怎么拿」，缓存行为与页面无需改。 */
export const KNOWLEDGE_BASE_TREE_QUERY_KEY = ['knowledge-bases', 'tree'] as const

/**
 * 列表页两级树（阶段 2：控制面一次拿全，见 #89）。
 *
 * 签名**刻意保持** `Promise<KnowledgeBaseNode[]>`：页面、轮询、
 * `KNOWLEDGE_BASE_TREE_QUERY_KEY` 都只认这个类型与这个函数，所以从阶段 1
 * （1 次列库 + 每库 1 次列文档）切到一次请求时，`KnowledgeFileTreePage.vue` 与
 * `useTreePolling.ts` 一行都不用改——变的是「怎么拿」而不是「拿到什么」。
 *
 * 为什么页面能安全地用会话身份读树：路由守卫（#85）已让列表页只在登录控制面后
 * 才可达，未登录会被跳 `/login`；控制面树按会话租户取全量，不按某把 Key 的白名单收窄。
 */
export async function fetchTree(): Promise<KnowledgeBaseNode[]> {
  return toKnowledgeBaseNodes(await fetchConsoleTree())
}
