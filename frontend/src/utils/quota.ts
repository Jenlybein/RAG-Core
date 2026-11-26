/**
 * 额度条的读数规则（三本账共用）。
 *
 * 抽成纯函数是因为这里有四个容易写错的判断，而且每一个都只在边界上出错；对应五种状态：
 *
 * - **剩余**：`limit - used`，已用超上限时夹到 0（不能让面板出现负数余额）；
 * - **未统计**（`untracked`）：`used === null` 是「限流关着、平台不知道」，与 `used === 0`
 *   （查过确实是 0）是两件事，不能合并；
 * - **未开通**（`off`）：`limit <= 0` 表示这一档套餐不含这本账（free 档的 rerank / llm 就是 0），
 *   此时谈「剩余多少」没有意义；
 * - **没有数据**（`missing`）：接口根本没给这一栏（后端旧版本 / 部分失败）。这与「未统计」
 *   也不同：那个是「开关关着、读不到数」，这个是「连这一栏都没有」；
 * - **百分比**：`limit <= 0` 时除法无意义，返回 `null` 而不是 `Infinity`。
 */
import type { ConsoleUsage } from '../types/console'

/**
 * 印在条形上的状态。
 *
 * `normal` / `near` / `out` 是三种读数状态；`off` = 套餐不含这本账；
 * `untracked` = 读数未知（开关没开）；`missing` = 接口没给这一栏。
 */
export type QuotaState = 'normal' | 'near' | 'out' | 'off' | 'untracked' | 'missing'

/**
 * 「接近上限」的分档线：面板自己的视觉分档，**与告警阈值 `USAGE_ALERT_QUOTA_PCT`
 * 无关**——那个是管理员页用来算「需要看一眼」的运行时配置，会热改；面板这条只是
 * 「条变黄」的门槛，不参与任何判定，也不必跟着它变。
 */
export const NEAR_LIMIT_RATIO = 0.8

/** 剩余：上限 − 已用，夹到 0；未统计 / 未开通 / 没有数据时返回 `null`。 */
export function remainingOf(usage: ConsoleUsage | null): number | null {
  if (usage === null || usage.used === null || usage.limit <= 0) return null
  return Math.max(0, usage.limit - usage.used)
}

/**
 * 已用**百分比**（0–100 的整数，不是 0–1 的比值）。
 *
 * 名字里带 `Percent` 就是提醒这件事：后端 `console_usage` 的 `percent` 是比值，
 * 混用会差一百倍（`utils/usageFormat.ts` 的 `formatRatio` 收的正是比值）。
 * 未统计 / 未开通 / 没有数据时返回 `null`。
 */
export function usedPercentOf(usage: ConsoleUsage | null): number | null {
  if (usage === null || usage.used === null || usage.limit <= 0) return null
  return Math.min(100, Math.round((usage.used / usage.limit) * 100))
}

export function stateOf(usage: ConsoleUsage | null): QuotaState {
  // 判定顺序就是上面四条：先问「有没有这一栏」，再问「这一档含不含」，最后才看读数。
  if (usage === null) return 'missing'
  if (usage.limit <= 0) return 'off'
  if (usage.used === null) return 'untracked'
  if (usage.used >= usage.limit) return 'out'
  if (usage.used / usage.limit >= NEAR_LIMIT_RATIO) return 'near'
  return 'normal'
}

/**
 * 这一状态的主位写的是**词或占位符**（「未统计」/「未开通」/「—」）而不是读数。
 *
 * 词要用正文字号与灰色：按 17px/600 排成数字的样子，读者会把它当成一个读数读过去。
 */
export function isWordState(state: QuotaState): boolean {
  return state === 'untracked' || state === 'off' || state === 'missing'
}

/**
 * 这一状态有没有「剩余 / 上限 / 条」可画。
 *
 * 未开通（这一档没有这一项）与没有数据（接口没给）都画不出额度，只有一条名称；
 * 「未统计」例外：上限是知道的，所以它还是一条带虚线空槽的行。
 */
export function showsMeter(state: QuotaState): boolean {
  return state !== 'off' && state !== 'missing'
}
