import { consoleHttp } from '../lib/consoleHttp'

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
  total: number
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

export function observabilityOverviewQueryKey(hours: number): readonly unknown[] {
  return ['console', 'admin', 'observability', 'overview', hours]
}

export function observabilityStagesQueryKey(hours: number): readonly unknown[] {
  return ['console', 'admin', 'observability', 'stages', hours]
}

export function observabilityStackQueryKey(): readonly unknown[] {
  return ['console', 'admin', 'observability', 'stack']
}

export function fetchObservabilityOverview(hours: number): Promise<ObservabilityOverview> {
  return consoleHttp.get<ObservabilityOverview>('/console/admin/observability/overview', {
    params: { hours },
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
