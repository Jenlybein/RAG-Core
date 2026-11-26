import { consoleHttp } from '../lib/consoleHttp'
import type { UsageScope } from '../types/usage'

/**
 * 管理端「观测总览」的三个只读端点（`/console/admin/observability/*`）。
 *
 * 两层数据分开取、分开显示，**不在前端合并**：
 *
 * - `overview` 来自我们自己的库（`retrieval_traces` / `retrieval_feedback`），零外部依赖；
 * - `stages` 来自观测栈，可能取不到（`source: 'none'`）——那时页面显示「未接入 / 不可达」，
 *   而不是画一屏 0 毫秒。
 *
 * 两条口径提醒（后端注释里也有，前端这里同样要认）：
 * - `degraded_rate` 为 `null` 表示**没有数据**，不是「没有问题」；
 * - `latency.p50/p95` 为 `null` 同理，别用 `?? 0` 折成 0。
 */

export const OBS_WINDOWS = [1, 6, 24, 72, 168] as const
export type ObsWindow = (typeof OBS_WINDOWS)[number]

export interface ObservabilityOverview {
  window_hours: number
  since: string
  /** 三段式：全部 / 只看成功 / 只看失败（第 14 步）。 */
  scope: UsageScope
  /**
   * 降级率 / 空结果率在这一档里**有没有意义**。
   *
   * 只有 `ok` 为真：被挡下的请求既不降级也不是空结果。另外两档要显示「—」而不是 0。
   */
  rates_meaningful: boolean
  total: number
  /** 成功数（code = 0）与失败数（code 非 0，不含「未统计」）。 */
  ok: number
  failed: number
  unknown_code: number
  latency: { p50: number | null; p95: number | null; worst: number | null }
  degraded: number
  empty: number
  ratings: {
    histogram: Record<string, number>
    count: number
    average: number | null
  }
  by_tenant: Array<{
    tenant_id: string
    total: number
    degraded: number
    degraded_rate: number | null
    avg_latency_ms: number | null
  }>
  hourly: Array<{ bucket: string; total: number }>
}

export interface StageAggregate {
  name: string
  samples: number
  p50: number | null
  p95: number | null
  share: number | null
}

export interface ObservabilityStages {
  /** `langfuse` = 来自观测栈；`none` = 这次取不到（没接 / 凭据不全 / 不可达）。 */
  source: 'langfuse' | 'none'
  reason: string | null
  stages: StageAggregate[]
}

export interface ObservabilityStack {
  sink: string
  has_keys: boolean
  langfuse_host: string | null
  stack_layer_available: boolean
}

export function observabilityOverviewQueryKey(
  hours: number,
  scope: UsageScope = 'all',
): readonly unknown[] {
  return ['console', 'admin', 'observability', 'overview', hours, scope]
}

export function observabilityStagesQueryKey(hours: number): readonly unknown[] {
  return ['console', 'admin', 'observability', 'stages', hours]
}

export function observabilityStackQueryKey(): readonly unknown[] {
  return ['console', 'admin', 'observability', 'stack']
}

export function fetchObservabilityOverview(
  hours: number,
  scope: UsageScope = 'all',
): Promise<ObservabilityOverview> {
  return consoleHttp.get<ObservabilityOverview>('/console/admin/observability/overview', {
    params: { hours, scope },
  })
}

export function fetchObservabilityStages(hours: number): Promise<ObservabilityStages> {
  return consoleHttp.get<ObservabilityStages>('/console/admin/observability/stages', {
    params: { hours },
  })
}

export function fetchObservabilityStack(): Promise<ObservabilityStack> {
  return consoleHttp.get<ObservabilityStack>('/console/admin/observability/stack')
}
