/**
 * 控制面 `GET /console/me` 的响应形状（`app/schemas/console.py` 的 `MeData`）。
 *
 * 只声明本票用得到、且事实底座里给了字段的部分：套餐快照与用量在 T3/T13 才会被渲染，
 * 提前把它们写成一份「猜的契约」只会让下一票来改类型时以为后端变了。
 * 后端每加一列这里就会漏吗？不会——这里只取字段，多余的字段进不了前端；少字段会在
 * 使用处被 TS 拦住，比一个 `unknown` 更早暴露契约漂移。
 */

/** `accounts.role` 的取值：`admin` 是平台管理员，其余（缺省 `user`）都是租户账号。 */
export type ConsoleRole = 'admin' | 'user' | string

export interface ConsoleAccount {
  account_id: string
  username: string
  role: ConsoleRole
  status: string
  /** 管理员为 `null`：它不属于任何租户，用户面板接口对它一律 20011。 */
  tenant_id: string | null
}

export interface ConsoleTenant {
  tenant_id: string
  name: string
  status: string
  plan_id: number
}

/** `/console/me` 里的套餐快照（没有 `plan_id`，它在 `tenant` 那一格）。 */
export interface ConsolePlanSnapshot {
  code: string
  name: string
  allowed_profiles: string[]
  hybrid_allowed: boolean
  rerank_allowed: boolean
  query_rewrite_allowed: boolean
  research_allowed: boolean
  retrieve_qps: number
  retrieve_daily: number
  max_kb: number
  max_kb_per_retrieve: number
  max_documents_per_kb: number
  max_processing_documents: number
}

export interface ConsoleUsage {
  /** `null` 表示「限流关着、平台不在统计」，与 `0` 是两件事。 */
  used: number | null
  limit: number
  source: string
  note: string | null
}

export interface ConsoleMe {
  account: ConsoleAccount
  tenant: ConsoleTenant | null
  plan: ConsolePlanSnapshot | null
  usage: ConsoleUsage | null
}
