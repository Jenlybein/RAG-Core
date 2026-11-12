import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))

// 只替换「网络那一层」：被测的是服务模块对控制面契约的承诺（路径、请求体、次数）。
vi.mock('../../lib/consoleHttp', () => ({ consoleHttp: mocks }))

import type { KeyData, KeyPolicyInput } from '../../types/key'
import { createKey, deleteKey, fetchKeys, patchPolicy } from '../keyService'

beforeEach(() => {
  mocks.get.mockReset()
  mocks.post.mockReset()
  mocks.patch.mockReset()
  mocks.delete.mockReset()
})

const KEY_ROW: KeyData = {
  key_id: 'key_1',
  label: '集成 A',
  key_value: 'rc_live_0123456789abcdef',
  key_masked: 'rc_live_…ef',
  status: 'active',
  policy: {
    policy_id: 'policy_1',
    name: '集成 A',
    allowed_kb_ids: ['kb_1'],
    research_allowed: false,
    hybrid_allowed: true,
    rerank_allowed: false,
    query_rewrite_allowed: false,
    retrieve_qps: 10,
    retrieve_daily: 1000,
    profile: 'balanced',
    include_parent: true,
    expand_table_rows: true,
    custom_options: {},
  },
}

const POLICY_INPUT: KeyPolicyInput = {
  allowed_kb_ids: ['kb_1'],
  research_allowed: false,
  hybrid_allowed: true,
  rerank_allowed: false,
  query_rewrite_allowed: false,
  retrieve_qps: 10,
  retrieve_daily: 1000,
  profile: 'balanced',
  include_parent: true,
  expand_table_rows: true,
  custom_options: {},
}

describe('fetchKeys', () => {
  it('GET /console/keys 一次，把信封里的 keys 摊平返回', async () => {
    mocks.get.mockResolvedValue({ keys: [KEY_ROW] })

    await expect(fetchKeys()).resolves.toEqual([KEY_ROW])
    expect(mocks.get).toHaveBeenCalledTimes(1)
    expect(mocks.get.mock.calls[0]).toEqual(['/console/keys'])
  })
})

describe('createKey', () => {
  it('POST /console/keys，请求体是 {label, policy}；label 去掉首尾空白', async () => {
    mocks.post.mockResolvedValue(KEY_ROW)

    await createKey({ label: '  集成 A  ', policy: POLICY_INPUT })

    expect(mocks.post).toHaveBeenCalledTimes(1)
    const [url, body] = mocks.post.mock.calls[0]
    expect(url).toBe('/console/keys')
    // 期望值写死：policy 必须是完整一组，不能只发一个「看起来够了」的子集。
    expect(body).toEqual({ label: '集成 A', policy: POLICY_INPUT })
  })

  it('没有 label 时不发这个键，而不是发 null（请求体越干净越好）', async () => {
    mocks.post.mockResolvedValue(KEY_ROW)

    await createKey({ label: '   ', policy: POLICY_INPUT })

    const body = mocks.post.mock.calls[0][1]
    expect(Object.keys(body)).toEqual(['policy'])
  })
})

describe('deleteKey', () => {
  it('DELETE /console/keys/{key_id}，回执原样返回', async () => {
    const receipt = { key_id: 'key_1', deleted: true }
    mocks.delete.mockResolvedValue(receipt)

    await expect(deleteKey('key_1')).resolves.toEqual(receipt)
    expect(mocks.delete).toHaveBeenCalledWith('/console/keys/key_1')
  })
})

describe('patchPolicy', () => {
  it('PATCH /console/key-policies/{policy_id}，只发补丁里的字段', async () => {
    const result = { policy_id: 'policy_1', before: {}, after: {}, changed: ['retrieve_qps'] }
    mocks.patch.mockResolvedValue(result)

    await patchPolicy('policy_1', { retrieve_qps: 5 })

    expect(mocks.patch).toHaveBeenCalledTimes(1)
    const [url, body] = mocks.patch.mock.calls[0]
    expect(url).toBe('/console/key-policies/policy_1')
    // custom_options 是**部分合并**：只发这次改的键，不能整份替换。
    expect(body).toEqual({ retrieve_qps: 5 })
  })

  it('custom_options 的补丁按键原样透传（合并语义交给后端）', async () => {
    mocks.patch.mockResolvedValue({ policy_id: 'policy_1', before: {}, after: {}, changed: [] })

    await patchPolicy('policy_1', { custom_options: { top_k: 8 } })

    expect(mocks.patch.mock.calls[0][1]).toEqual({ custom_options: { top_k: 8 } })
  })
})
