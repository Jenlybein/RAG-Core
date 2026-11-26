import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))

// 只替换「网络那一层」：被测的是这一层对控制面契约的承诺（路径、参数、翻译）。
vi.mock('../../lib/consoleHttp', () => ({ consoleHttp: mocks }))

import {
  fetchSiteUsage,
  fetchTenantRanking,
  fetchTenantUsage,
  fetchTenantUsageDetail,
  normalizeWindow,
  toAlert,
  toKeyRow,
  toTenantRow,
  usageQueryKey,
} from '../usageService'

beforeEach(() => {
  mocks.get.mockReset()
})

const RAW_SERIES = {
  window: '1d' as const,
  bucket: '1h',
  since: '2026-11-23T15:00:00',
  until: '2026-11-24T15:00:00',
  partial_last_bucket: true,
  source: 'events' as const,
  p95_source: 'exact' as const,
  totals: {
    requests: 5,
    ok: 2,
    failed: 2,
    unknown_code: 1,
    success_rate: 0.5,
    p50_ms: 320,
    p95_ms: 812,
    meter_r: 5,
    meter_rr: 1,
    meter_llm: 0,
  },
  buckets: [
    {
      bucket: '15:00',
      requests: 1,
      ok: 1,
      failed: 0,
      meter_r: 1,
      meter_rr: 0,
      meter_llm: 0,
      p95_ms: 400,
    },
  ],
}

const RAW_KEY = {
  key_id: 'key_1',
  label: 'prod',
  masked: 'rc_live_…4f3a2',
  deleted: false,
  requests: 5,
  ok: 5,
  failed: 0,
  success_rate: 1,
}

describe('normalizeWindow', () => {
  it('keeps the four whitelisted windows', () => {
    expect(normalizeWindow('1h')).toBe('1h')
    expect(normalizeWindow('1d')).toBe('1d')
    expect(normalizeWindow('7d')).toBe('7d')
    expect(normalizeWindow('30d')).toBe('30d')
  })

  it('falls back to 1d for anything else instead of failing', () => {
    // URL 手改成 last-quarter / 空值时，页面不该白屏；后端也会再收敛一次。
    expect(normalizeWindow('last-quarter')).toBe('1d')
    expect(normalizeWindow(null)).toBe('1d')
    expect(normalizeWindow(undefined)).toBe('1d')
    expect(normalizeWindow('')).toBe('1d')
  })
})

describe('snake_case → camelCase', () => {
  it('translates a key row', () => {
    const row = toKeyRow({ ...RAW_KEY, success_rate: null })

    expect(row.keyId).toBe('key_1')
    expect(row.masked).toBe('rc_live_…4f3a2')
    // null 原样保留：「没有数据」不许被折成 0。
    expect(row.successRate).toBeNull()
  })

  it('translates a tenant row', () => {
    const row = toTenantRow({
      tenant_id: 'tenant_a',
      requests: 3,
      ok: 3,
      failed: 0,
      success_rate: 1,
      p95_ms: 300,
    })

    expect(row.tenantId).toBe('tenant_a')
    expect(row.p95Ms).toBe(300)
  })

  it('keeps the alert level and kind untouched', () => {
    expect(toAlert({ kind: 'quota_full', level: 'error', message: 'x' })).toEqual({
      kind: 'quota_full',
      level: 'error',
      message: 'x',
    })
  })
})

describe('fetchTenantUsage', () => {
  it('maps the series and keeps the unknown-outcome count', async () => {
    mocks.get.mockResolvedValue({ ...RAW_SERIES, keys: [RAW_KEY] })

    const view = await fetchTenantUsage('1d')

    const [path, config] = mocks.get.mock.calls[0] as [string, { params: Record<string, string> }]
    expect(path).toBe('/console/usage')
    // 不带 key_id 时**不传**这个参数：它不是「空字符串」，而是「不筛」。
    expect(config.params).toEqual({ window: '1d' })
    expect(view.totals.unknownCode).toBe(1)
    expect(view.totals.successRate).toBe(0.5)
    expect(view.buckets[0].p95Ms).toBe(400)
    expect(view.keys[0].keyId).toBe('key_1')
    expect(view.partialLastBucket).toBe(true)
  })

  it('passes key_id when narrowing to one key', async () => {
    mocks.get.mockResolvedValue({ ...RAW_SERIES, keys: [] })

    await fetchTenantUsage('7d', 'key_1')

    const [, config] = mocks.get.mock.calls[0] as [string, { params: Record<string, string> }]
    expect(config.params).toEqual({ window: '7d', key_id: 'key_1' })
  })

  it('does not send an empty key_id', async () => {
    mocks.get.mockResolvedValue({ ...RAW_SERIES, keys: [] })

    await fetchTenantUsage('1d', '')

    const [, config] = mocks.get.mock.calls[0] as [string, { params: Record<string, string> }]
    expect(config.params).toEqual({ window: '1d' })
  })
})

describe('the admin endpoints', () => {
  it('reads the site series with alerts', async () => {
    mocks.get.mockResolvedValue({
      ...RAW_SERIES,
      alerts: [{ kind: 'no_traffic', level: 'info', message: '没有调用' }],
    })

    const view = await fetchSiteUsage('30d')

    const [path, config] = mocks.get.mock.calls[0] as [string, { params: Record<string, string> }]
    expect(path).toBe('/console/admin/usage/site')
    expect(config.params).toEqual({ window: '30d' })
    expect(view.alerts[0].kind).toBe('no_traffic')
  })

  it('reads the tenant ranking', async () => {
    mocks.get.mockResolvedValue({
      window: '7d',
      source: 'daily',
      tenants: [
        { tenant_id: 'a', requests: 2, ok: 1, failed: 1, success_rate: 0.5, p95_ms: 300 },
      ],
    })

    const ranking = await fetchTenantRanking('7d')

    expect(ranking.source).toBe('daily')
    expect(ranking.tenants[0].tenantId).toBe('a')
  })

  it('url-encodes the tenant id when drilling down', async () => {
    mocks.get.mockResolvedValue({ ...RAW_SERIES, tenant_id: 'tenant/a b', keys: [] })

    await fetchTenantUsageDetail('tenant/a b', '1d')

    const [path] = mocks.get.mock.calls[0] as [string]
    expect(path).toBe('/console/admin/usage/tenants/tenant%2Fa%20b')
  })
})

describe('usageQueryKey', () => {
  it('separates the five kinds and the window', () => {
    expect(usageQueryKey('site', '1d')).toEqual(['console', 'usage', 'site', '1d'])
    expect(usageQueryKey('key', '1d', 'k1')).toEqual(['console', 'usage', 'key', '1d', 'k1'])
    // 不同 kind / 不同窗口必须是不同的 key，否则 vue-query 会把两屏的数据串起来。
    expect(usageQueryKey('site', '1d')).not.toEqual(usageQueryKey('tenant', '1d'))
    expect(usageQueryKey('site', '1d')).not.toEqual(usageQueryKey('site', '7d'))
  })
})
