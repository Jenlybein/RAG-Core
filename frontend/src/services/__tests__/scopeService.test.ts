import { describe, expect, it, vi } from 'vitest'

/**
 * `scopeService` 必须打**数据面**那个只读端点，且**不带任何查询参数**。
 *
 * 为什么要单独钉：这条调用是「选 Key → 问服务端能查哪些库」的唯一真相来源。如果哪天
 * 有人把它改成控制面的 `/console/knowledge-bases/tree`，类型照样过、页面照样能跑，
 * 但答案会从「服务端按这把 Key 过滤后的结果」退化成「会话身份看到的全部库」——
 * 那是这次改动要消灭的东西，而且**退化时不会有任何报错**。
 */

const { get } = vi.hoisted(() => ({ get: vi.fn() }))

vi.mock('../../lib/dataHttp', () => ({ dataHttp: { get } }))

const { fetchMyKnowledgeBases } = await import('../scopeService')

describe('scopeService：问服务端「这把 Key 能查哪些库」', () => {
  it('打的是数据面的 GET /api/v1/knowledge-bases', async () => {
    get.mockResolvedValue({ knowledge_bases: [] })

    await fetchMyKnowledgeBases()

    // 路径与「没有多余参数」一起断言：身份由请求拦截器注入，调用方不该自己拼东西。
    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/api/v1/knowledge-bases')
  })

  it('把服务端返回的清单原样交出去（不在服务层再筛一遍）', async () => {
    const payload = {
      knowledge_bases: [
        {
          kb_id: 'kb-a',
          name: '演示库',
          tenant_id: 'tenant_beta',
          split_strategy: 'fixed',
          allowed_formats: ['md', 'pdf'],
          chunk_size: 512,
        },
      ],
    }
    get.mockResolvedValue(payload)

    // 服务端已经是过滤后的答案；这一层再筛一次就会出现第二个「谁知道真相」。
    await expect(fetchMyKnowledgeBases()).resolves.toBe(payload)
  })
})
