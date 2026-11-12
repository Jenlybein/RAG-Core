/**
 * 数据面接口清单：调试台要能一眼看出「这把 Key 能调什么、受什么限制」。
 *
 * 写成纯数据 + 纯函数而不是直接铺在组件里，理由与仓库其它地方一致：这份清单是**契约**
 * （端点、方法、门禁），它该被测试读、被 diff 看到；而怎么画是组件的事。
 *
 * 三条不能含糊的规矩：
 *
 * 1. **只读的才提供「试一下」**。写入类端点（上传 / 建库 / 删除 / 重建索引）在这里只
 *    列出、标清楚「控制台不代跑」——调试台拿的是真实 Key、打的是真实租户的数据，
 *    一个误点就会真的改变数据。要看写操作怎么用，去知识库管理页做（那里有确认与回执）。
 * 2. **每条都要写清「受什么限制」**。同一个 20008，可能是库不在白名单、也可能是库不存在；
 *    不写清楚，用户只能靠猜。
 * 3. **路径写死成字符串**。它们是后端路由表的一部分，改路由时这里会跟着红（有测试钉着
 *    条数与内容），不会悄悄漂。
 */

/** 可以真跑一次的只读探测；`null` 表示这条不提供试跑。 */
export type TrialKind = 'knowledge-bases' | 'auth-me' | 'documents'

export interface DataPlaneEndpoint {
  method: 'GET' | 'POST' | 'DELETE'
  /** 与后端路由表逐字对应（不带 `/api/v1` 前缀，便于阅读）。 */
  path: string
  /** 一句话说明它是干什么的。 */
  purpose: string
  /** 路由里哪些限制会作用在它身上；写操作额外注明「写」。 */
  gate: string
  /** 可试跑的只读探测；写操作一律没有。 */
  trial?: TrialKind
  /** 试跑需要额外输入时说明它从哪来（例如「用当前勾选的第一个库」）。 */
  trialNeeds?: string
  /** 写操作：控制台**不代跑**，只列出。 */
  write?: boolean
}

export const DATA_PLANE_ENDPOINTS: readonly DataPlaneEndpoint[] = [
  {
    method: 'POST',
    path: '/rag/retrieve',
    purpose: '检索（本页上方那块就是它）',
    gate: '白名单 → 套餐 → 档位 → 限流 → 日配额，五道门禁全过才召回',
  },
  {
    method: 'POST',
    path: '/rag/feedback',
    purpose: '给某一次检索打分（1~5）',
    gate: '评分按 Key 解出的租户落库；同一个 log_id 重复打是覆盖',
  },
  {
    method: 'GET',
    path: '/knowledge-bases',
    purpose: '这把 Key 可见的库（本页「要查的知识库」用的就是它）',
    gate: '租户过滤 + 按这把 Key 的数据范围再收窄（越界的直接不出现）',
    trial: 'knowledge-bases',
  },
  {
    method: 'GET',
    path: '/auth/me',
    purpose: '这把 Key 是谁：租户、套餐、今日用量',
    gate: '只读；用量开关关着时如实报「未统计」，不是 0',
    trial: 'auth-me',
  },
  {
    method: 'GET',
    path: '/documents',
    purpose: '某个库下的文档与索引进度',
    gate: '需要 kb_id；库不可见时按「不存在」处理',
    trial: 'documents',
    trialNeeds: '用当前勾选的第一个库',
  },
  {
    method: 'POST',
    path: '/knowledge-bases/create',
    purpose: '建库',
    gate: '写：套餐的库数上限',
    write: true,
  },
  {
    method: 'POST',
    path: '/documents/upload',
    purpose: '上传文档并异步索引',
    gate: '写：格式白名单、套餐的文档数上限',
    write: true,
  },
  {
    method: 'POST',
    path: '/documents/{id}/reindex',
    purpose: '重建单个文档的索引',
    gate: '写：只影响这一个文档',
    write: true,
  },
  {
    method: 'DELETE',
    path: '/documents/{id}',
    purpose: '删除文档',
    gate: '写：不可撤销',
    write: true,
  },
]

/**
 * 这条现在能不能试跑。
 *
 * `documents` 需要库；调用方把「当前勾选的第一个库」传进来。**没有库就不给按钮**：
 * 按钮点下去必然失败的那种「能点但没用」，比没有按钮更糟。
 */
export function canTrial(
  endpoint: DataPlaneEndpoint,
  { kbId }: { kbId: string | null },
): boolean {
  if (endpoint.trial === undefined) return false
  if (endpoint.trial === 'documents') return kbId !== null && kbId !== ''
  return true
}
