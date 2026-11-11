import { consoleHttp } from '../lib/consoleHttp'
import type {
  AccountListData,
  AccountPatchData,
  AccountPatchRequest,
  AuditLogListData,
  PlanListData,
  PlanPatchBody,
  PlanPatchData,
} from '../types/consoleAdmin'

/**
 * 管理员面板三个页面的请求层（`/console/accounts`、`/console/plans`、`/console/audit-logs`）。
 *
 * 全部走控制面客户端 `consoleHttp`：身份是会话 cookie，写请求由客户端自动补
 * `x-requested-with`。**不能用数据面的 Bearer 实例**——那等于把「用 API Key 管平台」
 * 写进代码，后端也会直接 20011。
 *
 * 这里只负责「怎么请求」；什么时候请求、怎么缓存由页面的 `useQuery` / mutation 决定。
 */

/** 账号列表的 query key：写操作成功后页面按它失效。 */
export const ACCOUNTS_QUERY_KEY = ['console', 'accounts'] as const

/** 套餐列表的 query key。账号页的换套餐下拉与套餐页共用同一份缓存。 */
export const PLANS_QUERY_KEY = ['console', 'plans'] as const

/**
 * 审计列表的 query key。`limit` 是后端唯一的分页参数（没有 offset），所以它是 key 的一部分：
 * 改条数就是另一次请求，不该命中旧条数的那份缓存。
 */
export function auditLogsQueryKey(limit: number, offset: number, action: string) {
  // 三个参数都进 key：翻页、换页大小、换动作过滤各自是一份独立的缓存，
  // 少一个就会出现「翻到第 2 页却拿到第 1 页的缓存」。
  return ['console', 'audit-logs', limit, offset, action] as const
}

/** 管理员跨租户看全部账号；每行带所属租户与该租户当前的套餐。 */
export function fetchAccounts(): Promise<AccountListData> {
  return consoleHttp.get<AccountListData>('/console/accounts')
}

/** 全部套餐（按 `updated_at` 的时间语义展示，不做前端排序）。 */
export function fetchPlans(): Promise<PlanListData> {
  return consoleHttp.get<PlanListData>('/console/plans')
}

/**
 * 改一个账号：停用/启用账号、停用/启用它所属租户、换该租户的套餐。
 *
 * 三项字段可同时发，后端先全量校验再落库；`undefined` 的键会被 JSON 丢掉，
 * 与「这次不动它」等价。
 */
export function updateAccount(
  accountId: string,
  patch: AccountPatchRequest,
): Promise<AccountPatchData> {
  return consoleHttp.patch<AccountPatchData>(
    `/console/accounts/${encodeURIComponent(accountId)}`,
    patch,
  )
}

/**
 * 改套餐。`dryRun=true` 只试算、不落库（`impact` 非空）；`false` 才真正写入。
 *
 * 两步保存的产品规则放在 `views/admin/PlansPage.vue`：先 `true` 看影响面，必要时确认，
 * 再 `false` 落库。服务层不替调用方决定这一点，它只把 `dry_run` 如实传过去。
 */
export function updatePlan(
  planId: number,
  patch: PlanPatchBody,
  dryRun: boolean,
): Promise<PlanPatchData> {
  return consoleHttp.patch<PlanPatchData>(`/console/plans/${planId}`, patch, {
    params: { dry_run: dryRun },
  })
}

/**
 * 删除套餐。
 *
 * `dry_run` 显式写 `false`：后端的默认值是 `true`（「忘了传参数时最坏只是没保存」），
 * 省略它的话这次「删除」只会试算并返回 `deleted=true`，库里的行还在——一个看起来成功、
 * 实际什么都没发生的删除。被租户引用时后端返回 `20019`，这里不吞掉它。
 */
export function deletePlan(planId: number): Promise<PlanPatchData> {
  return updatePlan(planId, { operation: 'delete' }, false)
}

/**
 * 审计列表。`limit` 是后端唯一支持的参数（缺省 50、上限 200）；没有 offset/动作过滤接口，
 * 所以「按动作过滤」在页面里对已载入的这批记录做，不伪造一个后端不存在的分页协议。
 */
export function fetchAuditLogs(
  limit: number,
  offset: number,
  action: string,
): Promise<AuditLogListData> {
  // 分页与动作过滤都在后端做：`offset` 是服务端分页，不是「先拉全量再切片」。
  // `action` 传空串时**不放进参数**——后端把它当成「不过滤」，传空串反而会去匹配空动作。
  const params: Record<string, string | number> = { limit, offset }
  if (action !== '') params.action = action
  return consoleHttp.get<AuditLogListData>('/console/audit-logs', { params })
}
