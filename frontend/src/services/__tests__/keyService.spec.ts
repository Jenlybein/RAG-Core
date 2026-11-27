import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))

// 只替换「网络那一层」：被测的是服务模块对控制面契约的承诺（路径、请求体、次数）。
vi.mock('../../lib/consoleHttp', () => ({ consoleHttp: mocks }))

import type { IssuedKeyData, KeyData, KeyPolicyInput } from '../../types/key'
import {
  createKey,
  deleteKey,
  fetchKeys,
  patchPolicy,
  regenerateKey,
  revokePreviousKey,
} from '../keyService'

beforeEach(() => {
  mocks.get.mockReset()
  mocks.post.mockReset()
  mocks.patch.mockReset()
  mocks.delete.mockReset()
})

const KEY_ROW: KeyData = {
  key_id: 'key_1',
  label: '集成 A',
  key_masked: 'rc_live_…4f3a2',
  status: 'active',
  prev_key_masked: null,
  prev_key_expires_at: null,
  rotated_at: null,
  policy: {
    policy_id: 'policy_1',
    name: '集成 A',
    allowed_kb_ids: ['kb_1'],
    retrieve_allowed: true,
    rewrite_allowed: false,
    decompose_allowed: false,
    research_allowed: false,
    rerank_allowed: false,
    retrieve_qps: 10,
    retrieve_daily: 1000,
    rerank_qps: 0,
    rerank_daily: 0,
    llm_qps: 0,
    llm_daily: 0,
    retrieval_mode: 'hybrid',
    retrieval_top_k: 5,
    retrieval_rerank: false,
    include_parent: true,
    expand_table_rows: true,
  max_unit_chars: 2000,
  },
}

const POLICY_INPUT: KeyPolicyInput = {
  allowed_kb_ids: ['kb_1'],
  retrieve_allowed: true,
  rewrite_allowed: false,
  decompose_allowed: false,
  research_allowed: false,
  rerank_allowed: false,
  retrieve_qps: 10,
  retrieve_daily: 1000,
  rerank_qps: 0,
  rerank_daily: 0,
  llm_qps: 0,
  llm_daily: 0,
  retrieval_mode: 'hybrid',
  retrieval_top_k: 5,
  retrieval_rerank: false,
  include_parent: true,
  expand_table_rows: true,
  max_unit_chars: 2000,
}

/** 签发响应：列表那一行 + 只此一次的原文（库里只有 scrypt 哈希）。 */
const ISSUED_KEY: IssuedKeyData = {
  ...KEY_ROW,
  key_plaintext: 'rc_live_key_1.' + 'a'.repeat(32),
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
    mocks.post.mockResolvedValue(ISSUED_KEY)

    const created = await createKey({ label: '  集成 A  ', policy: POLICY_INPUT })

    // 原文随响应回来一次，服务层不裁剪、不改写：它交给弹窗原文显示。
    expect(created.key_plaintext).toBe(ISSUED_KEY.key_plaintext)

    expect(mocks.post).toHaveBeenCalledTimes(1)
    const [url, body] = mocks.post.mock.calls[0]
    expect(url).toBe('/console/keys')
    // 期望值写死：policy 必须是完整一组，不能只发一个「看起来够了」的子集。
    expect(body).toEqual({ label: '集成 A', policy: POLICY_INPUT })
  })

  it('没有 label 时不发这个键，而不是发 null（请求体越干净越好）', async () => {
    mocks.post.mockResolvedValue(ISSUED_KEY)

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
    // 部分更新：只发这次改的键，不能整份替换。
    expect(body).toEqual({ retrieve_qps: 5 })
  })

  it('直参数的补丁按字段原样透传（三项各自独立可改）', async () => {
    mocks.patch.mockResolvedValue({ policy_id: 'policy_1', before: {}, after: {}, changed: [] })

    await patchPolicy('policy_1', { retrieval_top_k: 8 })

    expect(mocks.patch.mock.calls[0][1]).toEqual({ retrieval_top_k: 8 })
  })
})

describe('regenerateKey', () => {
  it('POST /console/keys/{key_id}/regenerate，不发明请求体，回执原样返回', async () => {
    mocks.post.mockResolvedValue({
      ...ISSUED_KEY,
      prev_key_masked: 'rc_live_…00000',
      prev_key_expires_at: '2026-11-27T12:00:00+08:00',
      rotated_at: '2026-11-26T12:00:00+08:00',
    })

    const regenerated = await regenerateKey('key_1')

    expect(mocks.post).toHaveBeenCalledTimes(1)
    // 路径写死：轮换与「签发一把新的」是两个端点，混用会把「多加一把」做成「换凭证」。
    expect(mocks.post.mock.calls[0][0]).toBe('/console/keys/key_1/regenerate')
    expect(mocks.post.mock.calls[0][1]).toBeUndefined()
    // 回执里三个过渡字段照原样透出：界面靠它们判断窗口还在不在。
    expect(regenerated.prev_key_expires_at).toBe('2026-11-27T12:00:00+08:00')
    expect(regenerated.rotated_at).toBe('2026-11-26T12:00:00+08:00')
    expect(regenerated.key_plaintext).toBe(ISSUED_KEY.key_plaintext)
  })
})

describe('revokePreviousKey', () => {
  it('POST /console/keys/{key_id}/revoke-previous，revoked=false 也照常返回（幂等）', async () => {
    mocks.post.mockResolvedValue({ key_id: 'key_1', revoked: false })

    await expect(revokePreviousKey('key_1')).resolves.toEqual({ key_id: 'key_1', revoked: false })
    expect(mocks.post.mock.calls[0][0]).toBe('/console/keys/key_1/revoke-previous')
  })
})
