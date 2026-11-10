import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }))

// 只替换「网络那一层」：被测的是这个服务模块对后端契约的承诺，不是 axios 的行为。
vi.mock('../../lib/dataHttp', () => ({
  dataHttp: {
    post: mocks.post,
    get: mocks.get,
  },
}))

import { fetchPickableKnowledgeBases, retrieve } from '../ragService'

beforeEach(() => {
  mocks.post.mockReset()
  mocks.get.mockReset()
})

/**
 * 请求体只有 `kb_ids` + `query`（00-facts §4/§5、ADR-0010「第三次修订」）。
 *
 * 这里用 `toEqual` 而不是逐个 `toHaveBeenCalledWith` 的部分匹配：后者允许请求体里
 * 多出字段而不报警，而「多带一个质量字段」正是会被后端 20002 拒掉的形态。
 * 期望值写死字面量，不复用被测函数的入参对象（否则断言只是回显）。
 */
describe('retrieve', () => {
  it('POST /api/v1/rag/retrieve，请求体只有 kb_ids 与 query', async () => {
    mocks.post.mockResolvedValue({
      query: '退款多久到账',
      kb_ids: ['kb_1'],
      retrieved_chunks: [],
      metadata: {},
    })

    await retrieve({ kb_ids: ['kb_1'], query: '退款多久到账' })

    expect(mocks.post).toHaveBeenCalledTimes(1)
    const [url, body] = mocks.post.mock.calls[0]
    expect(url).toBe('/api/v1/rag/retrieve')
    expect(body).toEqual({ kb_ids: ['kb_1'], query: '退款多久到账' })
    // 质量字段一个都不许出现：它们只能来自这把 Key 的密钥配置。
    expect(Object.keys(body)).toEqual(['kb_ids', 'query'])
  })
})

describe('fetchPickableKnowledgeBases', () => {
  it('GET /api/v1/knowledge-bases，并把信封里的 knowledge_bases 摊平返回', async () => {
    const item = {
      kb_id: 'kb_1',
      name: '产品手册',
      tenant_id: 'tenant_1',
      split_strategy: 'structure',
      allowed_formats: ['markdown'],
      chunk_size: 512,
    }
    mocks.get.mockResolvedValue({ knowledge_bases: [item] })

    await expect(fetchPickableKnowledgeBases()).resolves.toEqual([item])
    expect(mocks.get.mock.calls[0][0]).toBe('/api/v1/knowledge-bases')
  })
})
