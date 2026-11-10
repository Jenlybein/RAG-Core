import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ consoleGet: vi.fn(), dataGet: vi.fn() }))

// 两个客户端的 get 都换成 spy：断言「只发 1 个请求」必须同时看到两扇门，
// 否则老实现（数据面 1 + N 次）可能悄悄混进来。
vi.mock('../../lib/consoleHttp', () => ({
  consoleHttp: { get: mocks.consoleGet },
}))

vi.mock('../../lib/dataHttp', () => ({
  dataHttp: { get: mocks.dataGet },
}))

import type { ConsoleTreeData } from '../../types/knowledgeBase'
import { fetchTree } from '../knowledgeBaseService'

beforeEach(() => {
  mocks.consoleGet.mockReset()
  mocks.dataGet.mockReset()
})

/**
 * 验收里的「网络面板只发 1 个请求」在这里用单测钉死：更便宜，也能在 CI 里跑。
 *
 * 旧实现是 1 次 `GET /knowledge-bases` + 每个库 1 次 `GET /documents`；只要本用例
 * 要求 `consoleGet` 恰好 1 次且数据面客户端 0 次，任何人把逐库拉文档加回来都会变红。
 * 期望的树写死字面量，由响应形状独立于实现给出。
 */
describe('fetchTree', () => {
  it('一次控制面树请求就拿到整棵树，不再按库逐次拉文档', async () => {
    const payload: ConsoleTreeData = {
      knowledge_bases: [
        {
          kb_id: 'kb_1',
          name: '产品手册',
          split_strategy: 'structure',
          allowed_formats: ['markdown'],
          chunk_size: 512,
          documents: [
            {
              document_id: 'doc_1',
              title: '退款政策.md',
              status: 3,
              chunk_count: 0,
              error_message: null,
              source_type: 'markdown',
              created_at: '2026-01-01T00:00:00Z',
            },
          ],
        },
        {
          kb_id: 'kb_2',
          name: '空库',
          split_strategy: 'size',
          allowed_formats: ['text'],
          chunk_size: 1024,
          documents: [],
        },
      ],
    }
    mocks.consoleGet.mockResolvedValue(payload)

    await expect(fetchTree()).resolves.toEqual([
      {
        kb_id: 'kb_1',
        name: '产品手册',
        split_strategy: 'structure',
        allowed_formats: ['markdown'],
        chunk_size: 512,
        documents: [
          {
            document_id: 'doc_1',
            title: '退款政策.md',
            status: 3,
            chunk_count: 0,
            error_message: null,
            source_type: 'markdown',
            created_at: '2026-01-01T00:00:00Z',
          },
        ],
      },
      {
        kb_id: 'kb_2',
        name: '空库',
        split_strategy: 'size',
        allowed_formats: ['text'],
        chunk_size: 1024,
        documents: [],
      },
    ])
    expect(mocks.consoleGet).toHaveBeenCalledTimes(1)
    expect(mocks.consoleGet.mock.calls[0]).toEqual(['/console/knowledge-bases/tree'])
    // 数据面一次都不许被调用：列表页已经整体切到控制面这一扇门。
    expect(mocks.dataGet).not.toHaveBeenCalled()
  })
})
