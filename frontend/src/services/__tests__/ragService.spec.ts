import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }))

// 只替换「网络那一层」：被测的是这个服务模块对后端契约的承诺，不是 axios 的行为。
vi.mock('../../lib/dataHttp', () => ({
  dataHttp: {
    post: mocks.post,
    get: mocks.get,
  },
}))

import * as ragServiceModule from '../ragService'
import {
  retrieve,
  rewriteRetrieve,
  decomposeRetrieve,
  research,
} from '../ragService'

beforeEach(() => {
  mocks.post.mockReset()
  mocks.get.mockReset()
})

const RESPONSE = {
  query: '退款多久到账',
  kb_ids: ['kb_1'],
  retrieved_chunks: [],
  metadata: {},
}

/**
 * 四条检索能力端点的请求体都只有 `kb_ids` + `query`（ADR-0010）。
 * 加工方式由端点表达，质量参数由 Key 配置表达，页面不拼任何质量字段。
 * `toEqual` 而非部分匹配：多带一个字段会被后端 20002 拒掉，必须当场发现。
 */
describe.each([
  ['retrieve', retrieve, '/api/v1/rag/retrieve'],
  ['rewriteRetrieve', rewriteRetrieve, '/api/v1/rag/rewrite'],
  ['decomposeRetrieve', decomposeRetrieve, '/api/v1/rag/decompose'],
  ['research', research, '/api/v1/rag/research'],
] as const)('%s', (_name, fn, path) => {
  it(`POST ${path}，请求体只有 kb_ids 与 query`, async () => {
    mocks.post.mockResolvedValue(RESPONSE)

    await fn({ kb_ids: ['kb_1'], query: '退款多久到账' })

    expect(mocks.post).toHaveBeenCalledTimes(1)
    const [url, body] = mocks.post.mock.calls[0]
    expect(url).toBe(path)
    expect(body).toEqual({ kb_ids: ['kb_1'], query: '退款多久到账' })
    expect(Object.keys(body)).toEqual(['kb_ids', 'query'])
  })
})

describe('数据面边界', () => {
  it('服务模块只导出四条检索端点（列库走控制面树，不是数据面）', () => {
    // #92 的边界：数据面客户端只发检索类请求；可勾选的库来自控制面 fetchTree()。
    expect(Object.keys(ragServiceModule).sort()).toEqual(
      ['decomposeRetrieve', 'research', 'retrieve', 'rewriteRetrieve'].sort(),
    )
  })
})
