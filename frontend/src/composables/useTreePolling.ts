import { useQuery } from '@tanstack/vue-query'
import { computed } from 'vue'

import { fetchTree, KNOWLEDGE_BASE_TREE_QUERY_KEY } from '../services/knowledgeBaseService'
import type { KnowledgeBaseNode } from '../types/knowledgeBase'

/**
 * 列表页轮询：degraded 版只需要「有处理中 → 3 秒一次」这一条规则，
 * 但它决定整页的网络节奏，所以单独写成纯函数（T15 要写进文档的那条规矩）：
 *
 * - 判定不依赖 `useQuery`、定时器或组件生命周期，vitest 直接钉返回值；
 * - 页面与 `refetchInterval` 都只调用它，改刷新策略只改这一处；
 * - `status=3` 是「已提交、后台索引」，不是失败，绝不能把它删掉。
 *
 * 返回 `false`（而不是 0 或 `null`）是 TanStack Query 的约定：`false` 明确表示停止轮询，
 * 而 `0` 会被当成「立刻再来一次」，变成一个打满后端的忙等循环。
 */
export const PROCESSING_STATUS = 3
export const POLL_INTERVAL_MS = 3000

export function shouldContinuePolling(
  tree: readonly KnowledgeBaseNode[] | null | undefined,
): number | false {
  if (tree === null || tree === undefined) return false
  for (const knowledgeBase of tree) {
    for (const document of knowledgeBase.documents) {
      if (document.status === PROCESSING_STATUS) return POLL_INTERVAL_MS
    }
  }
  return false
}

/**
 * 列表页的树查询 + 轮询出口。
 *
 * 为什么查询建在 composable 里而不是页面里：`refetchInterval` 的回调要读「当前这份树」，
 * 若由页面先建 query 再拿 data 传回来，就会绕一圈并在每次刷新时读到上一次的闭包值。
 * 交给回调自带的 query 对象读最新 data，轮询条件就始终与页面显示的是同一份数据。
 *
 * 返回值保持一层：`treeQuery` 与 `hasProcessing`。删除 / 重建成功后由页面 invalidate
 * `KNOWLEDGE_BASE_TREE_QUERY_KEY`，这里不掺业务动作。
 */
export function useTreePolling() {
  const treeQuery = useQuery({
    queryKey: KNOWLEDGE_BASE_TREE_QUERY_KEY,
    queryFn: fetchTree,
    // 注意：这里必须用函数形态。写成固定 3000 会让请求永远不停，
    // 而写成 computed 也会在「全部终态」后留下一个不会自动复活的定时器。
    refetchInterval: (query) => shouldContinuePolling(query.state.data),
  })

  const hasProcessing = computed(() => shouldContinuePolling(treeQuery.data.value) !== false)

  return { treeQuery, hasProcessing }
}
