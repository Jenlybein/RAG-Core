import { consoleHttp } from '../lib/consoleHttp'
import type {
  P95Source,
  SiteUsageView,
  TenantRankingView,
  TenantUsageDetailView,
  TenantUsageView,
  UsageAlert,
  UsageBucket,
  UsageKeyRow,
  UsageSeries,
  UsageSource,
  UsageTenantRow,
  UsageTotals,
  UsageWindow,
} from '../types/usage'

/**
 * 用量看板的四个只读端点（第 14 步 #203）。
 *
 * **后端回 snake_case，这里翻成 camelCase**：整份仓库的 Vue 侧都是 camelCase，若让页面
 * 直接读 `success_rate`，两套命名会混在同一个模板里。翻译只发生在这一个文件，页面与类型
 * 都看不到下划线；反过来（页面里出现 `_`）就是有人绕过了这一层。
 *
 * 三条口径照抄后端，不许在前端「修」：
 * - `success_rate` / `p50_ms` / `p95_ms` 为 `null` = **没有数据**（不是 0）；
 * - `p95_source = 'histogram'` = 长窗的近似值，页面要标出来；
 * - `unknown_code > 0` = 迁移之前的老行，它们进「调用次数」不进成功率的分母。
 */

interface RawBucket {
  bucket: string
  requests: number
  ok: number
  failed: number
  meter_r: number
  meter_rr: number
  meter_llm: number
  p95_ms: number | null
}

interface RawTotals {
  requests: number
  ok: number
  failed: number
  unknown_code: number
  success_rate: number | null
  p50_ms: number | null
  p95_ms: number | null
  meter_r: number
  meter_rr: number
  meter_llm: number
}

interface RawSeries {
  window: UsageWindow
  bucket: string
  since: string
  until: string
  partial_last_bucket: boolean
  source: UsageSource
  p95_source: P95Source
  totals: RawTotals
  buckets: RawBucket[]
}

interface RawKeyRow {
  key_id: string
  label: string | null
  masked: string | null
  deleted: boolean
  requests: number
  ok: number
  failed: number
  success_rate: number | null
}

interface RawTenantRow {
  tenant_id: string
  requests: number
  ok: number
  failed: number
  success_rate: number | null
  p95_ms: number | null
}

interface RawAlert {
  kind: UsageAlert['kind']
  level: UsageAlert['level']
  message: string
}

function toBucket(raw: RawBucket): UsageBucket {
  return {
    bucket: raw.bucket,
    requests: raw.requests,
    ok: raw.ok,
    failed: raw.failed,
    meterR: raw.meter_r,
    meterRr: raw.meter_rr,
    meterLlm: raw.meter_llm,
    p95Ms: raw.p95_ms,
  }
}

function toTotals(raw: RawTotals): UsageTotals {
  return {
    requests: raw.requests,
    ok: raw.ok,
    failed: raw.failed,
    unknownCode: raw.unknown_code,
    successRate: raw.success_rate,
    p50Ms: raw.p50_ms,
    p95Ms: raw.p95_ms,
    meterR: raw.meter_r,
    meterRr: raw.meter_rr,
    meterLlm: raw.meter_llm,
  }
}

function toSeries(raw: RawSeries): UsageSeries {
  return {
    window: raw.window,
    bucket: raw.bucket,
    since: raw.since,
    until: raw.until,
    partialLastBucket: raw.partial_last_bucket,
    source: raw.source,
    p95Source: raw.p95_source,
    totals: toTotals(raw.totals),
    buckets: raw.buckets.map(toBucket),
  }
}

export function toKeyRow(raw: RawKeyRow): UsageKeyRow {
  return {
    keyId: raw.key_id,
    label: raw.label,
    masked: raw.masked,
    deleted: raw.deleted,
    requests: raw.requests,
    ok: raw.ok,
    failed: raw.failed,
    successRate: raw.success_rate,
  }
}

export function toTenantRow(raw: RawTenantRow): UsageTenantRow {
  return {
    tenantId: raw.tenant_id,
    requests: raw.requests,
    ok: raw.ok,
    failed: raw.failed,
    successRate: raw.success_rate,
    p95Ms: raw.p95_ms,
  }
}

export function toAlert(raw: RawAlert): UsageAlert {
  return { kind: raw.kind, level: raw.level, message: raw.message }
}

/** 窗口只接受白名单里的值；别的（含 URL 手改）一律安静地落回 `1d`。 */
export function normalizeWindow(raw: string | null | undefined): UsageWindow {
  return raw === '1h' || raw === '7d' || raw === '30d' || raw === '1d' ? raw : '1d'
}

export function usageQueryKey(
  kind: 'tenant' | 'key' | 'site' | 'tenants' | 'tenant-detail',
  window: UsageWindow,
  detail?: string,
): readonly unknown[] {
  return detail === undefined
    ? ['console', 'usage', kind, window]
    : ['console', 'usage', kind, window, detail]
}

export async function fetchTenantUsage(
  window: UsageWindow,
  keyId?: string,
): Promise<TenantUsageView> {
  const raw = await consoleHttp.get<RawSeries & { keys: RawKeyRow[] }>('/console/usage', {
    params: keyId === undefined || keyId === '' ? { window } : { window, key_id: keyId },
  })
  return { ...toSeries(raw), keys: (raw.keys ?? []).map(toKeyRow) }
}

export async function fetchSiteUsage(window: UsageWindow): Promise<SiteUsageView> {
  const raw = await consoleHttp.get<RawSeries & { alerts: RawAlert[] }>(
    '/console/admin/usage/site',
    { params: { window } },
  )
  return { ...toSeries(raw), alerts: (raw.alerts ?? []).map(toAlert) }
}

export async function fetchTenantRanking(
  window: UsageWindow,
  limit = 20,
): Promise<TenantRankingView> {
  const raw = await consoleHttp.get<{
    window: UsageWindow
    source: UsageSource
    tenants: RawTenantRow[]
  }>('/console/admin/usage/tenants', { params: { window, limit } })
  return {
    window: raw.window,
    source: raw.source,
    tenants: (raw.tenants ?? []).map(toTenantRow),
  }
}

export async function fetchTenantUsageDetail(
  tenantId: string,
  window: UsageWindow,
): Promise<TenantUsageDetailView> {
  const raw = await consoleHttp.get<RawSeries & { tenant_id: string; keys: RawKeyRow[] }>(
    `/console/admin/usage/tenants/${encodeURIComponent(tenantId)}`,
    { params: { window } },
  )
  return { ...toSeries(raw), tenantId: raw.tenant_id, keys: (raw.keys ?? []).map(toKeyRow) }
}
