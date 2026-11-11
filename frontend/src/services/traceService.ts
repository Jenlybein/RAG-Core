import { consoleHttp } from '../lib/consoleHttp'

/**
 * 「最近检索」列表与观测配置（控制面）。
 *
 * 两条端点都只读、都走会话身份（`require_console_tenant`）：管理员 `tenant_id` 为空，
 * 被这两条端点一律 `20011` —— 它们回答的是「本租户发生过什么」，没有租户就没有「本租户」。
 *
 * **这里没有原文**：归属索引本来就不存问句与切片正文（ADR-0021 决策 1），所以列表页不需要
 * 任何额外脱敏，也就没有「一不小心把原文画到屏幕上」这种可能。
 */

export interface RecentTrace {
  log_id: string
  trace_id: string
  created_at: string | null
  latency_ms: number | null
  top_k: number | null
  kb_count: number
  degraded_reason: string | null
  empty_reason: string | null
  /** 已打的分数；**未评分是 `null`，不是 0**（1~5 的取值域里没有 0）。 */
  score: number | null
}

export interface RecentTracesPayload {
  items: RecentTrace[]
  limit: number
}

export interface ObservabilityInfo {
  /** `jsonl` / `langfuse` / `none`，原样来自 `TRACE_SINK`。 */
  trace_sink: string
  /**
   * 观测栈 UI 的地址；**只有去向真是 `langfuse` 时才非空**。
   *
   * 去向是 `jsonl` / `none` 时栈里没有这条 trace，给个链接只会点出 404 —— 所以这条由
   * 服务端决定，前端不自己拼。密钥一个都不在这里。
   */
  langfuse_host: string | null
}

export function recentTracesQueryKey(limit: number): readonly unknown[] {
  return ['console', 'retrieval-traces', 'recent', limit]
}

export function observabilityQueryKey(): readonly unknown[] {
  return ['console', 'observability']
}

export function fetchRecentTraces(limit = 20): Promise<RecentTracesPayload> {
  return consoleHttp.get<RecentTracesPayload>('/console/retrieval-traces/recent', {
    params: { limit },
  })
}

export function fetchObservability(): Promise<ObservabilityInfo> {
  return consoleHttp.get<ObservabilityInfo>('/console/observability')
}
