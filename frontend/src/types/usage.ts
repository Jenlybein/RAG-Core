/**
 * 用量看板的类型（第 14 步 #203）。
 *
 * 对着后端 `app/services/console_usage.py` 的返回形状写：那边是 `ApiResponse[dict]`，
 * 所以这里**是唯一的类型来源**——服务层照它解析，页面照它渲染。字段一旦对不上，
 * `vue-tsc` 会在编译期红，而不是等到页面上出现一个空的 `undefined`。
 *
 * 三条口径直接写进类型注释里，避免下一个人在组件里 `?? 0`：
 * `successRate` / `p50Ms` / `p95Ms` 为 `null` 是「没有数据」，不是 0。
 */

/** 时间窗白名单，顺序即 segmented 的显示顺序。 */
export type UsageWindow = '1h' | '1d' | '7d' | '30d'

export const USAGE_WINDOWS: readonly { value: UsageWindow; label: string }[] = [
  { value: '1h', label: '1 小时' },
  { value: '1d', label: '1 天' },
  { value: '7d', label: '7 天' },
  { value: '30d', label: '30 天' },
]

/** 数据从哪来：明细（精确分位）还是日汇总（直方图近似）。 */
export type UsageSource = 'events' | 'daily'
/**
 * P95 的成色：
 *
 * - `exact`：明细上 `percentile_cont` 算的；
 * - `histogram`：7 档桶反解（误差约 13%，页面标题要写「近似」）；
 * - `histogram_missing`：长窗读了日汇总却是 0，而明细里这段时间**有**调用——那是
 *   rollup 还没跑，「算不出来」而不是「没有样本」。
 */
export type P95Source = 'exact' | 'histogram' | 'histogram_missing'

export interface UsageBucket {
  /** 桶标签：`10m` 窗是 `14:30`，`1h` 窗是 `14:00`，日历天是 `11-23`。 */
  bucket: string
  requests: number
  ok: number
  failed: number
  meterR: number
  meterRr: number
  meterLlm: number
  /** 这一桶里没有样本时是 `null`。 */
  p95Ms: number | null
}

export interface UsageTotals {
  requests: number
  ok: number
  failed: number
  /** 结局未知（迁移之前没有业务码）的行数：它们进「调用次数」，不进成功率的分母。 */
  unknownCode: number
  successRate: number | null
  p50Ms: number | null
  p95Ms: number | null
  meterR: number
  meterRr: number
  meterLlm: number
}

export interface UsageAlert {
  kind: 'quota_full' | 'success_drop' | 'quota_pct' | 'no_traffic'
  level: 'error' | 'warn' | 'info'
  message: string
}

export interface UsageKeyRow {
  keyId: string
  label: string | null
  masked: string | null
  /** `true` = 这把 Key 已经删了，但账还在（历史行保留）。 */
  deleted: boolean
  requests: number
  ok: number
  failed: number
  successRate: number | null
}

export interface UsageTenantRow {
  tenantId: string
  requests: number
  ok: number
  failed: number
  successRate: number | null
  p95Ms: number | null
}

/** 一条时间序列 + 合计。四个接口里三个都是这个形状。 */
export interface UsageSeries {
  window: UsageWindow
  /** 桶宽：`10m` / `1h` / `1d`。 */
  bucket: string
  since: string
  until: string
  /** 最后一个桶永远没走完（这个小时 / 这十分钟 / 今天）。 */
  partialLastBucket: boolean
  source: UsageSource
  p95Source: P95Source
  totals: UsageTotals
  buckets: UsageBucket[]
}

export interface TenantUsageView extends UsageSeries {
  keys: UsageKeyRow[]
}

export interface SiteUsageView extends UsageSeries {
  alerts: UsageAlert[]
}

export interface TenantUsageDetailView extends UsageSeries {
  tenantId: string
  keys: UsageKeyRow[]
}

export interface TenantRankingView {
  window: UsageWindow
  source: UsageSource
  tenants: UsageTenantRow[]
}

/** 观测页的三段式：全部 / 只看成功 / 只看失败。 */
export type UsageScope = 'all' | 'ok' | 'failed'

export const USAGE_SCOPES: readonly { value: UsageScope; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'ok', label: '只看成功' },
  { value: 'failed', label: '只看失败' },
]
