import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ get: vi.fn() }))

// 只替换「网络那一层」：这里测的是服务模块对控制面契约的承诺（路径、次数、形状），
// 不是 axios 的行为。
vi.mock('../../lib/consoleHttp', () => ({
  consoleHttp: { get: mocks.get },
}))

import type { ConsoleTreeData } from '../../types/knowledgeBase'
import { fetchConsoleTree, toKnowledgeBaseNodes } from '../consoleKb'

beforeEach(() => {
  mocks.get.mockReset()
})

/**
 * 映射是「页面渲不渲得出来」的最后一道门：`KnowledgeFileTreePage` 会直接读
 * `node.documents.length`、遍历 `allowed_formats`，缺一个数组就是白屏。
 * 期望值全部写成字面量，不复用被测函数的中间对象（否则断言只是回显）。
 */
describe('toKnowledgeBaseNodes', () => {
  it('空库：documents 为 []，不是缺省值 undefined', () => {
    const data: ConsoleTreeData = {
      knowledge_bases: [
        {
          kb_id: 'kb_empty',
          name: '空库',
          split_strategy: 'size',
          allowed_formats: ['text'],
          chunk_size: 1024,
          documents: [],
        },
      ],
    }

    expect(toKnowledgeBaseNodes(data)).toEqual([
      {
        kb_id: 'kb_empty',
        name: '空库',
        split_strategy: 'size',
        allowed_formats: ['text'],
        chunk_size: 1024,
        documents: [],
      },
    ])
  })

  it('多个库：顺序与字段原样保留，文档逐项映射', () => {
    const data: ConsoleTreeData = {
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
          name: '工单记录',
          split_strategy: 'size',
          allowed_formats: ['text', 'pdf'],
          chunk_size: 1024,
          documents: [
            {
              document_id: 'doc_2',
              title: '工单.pdf',
              status: 2,
              chunk_count: 0,
              error_message: '解析失败：正文为空',
              source_type: 'pdf',
              created_at: '2026-01-02T00:00:00Z',
            },
            {
              document_id: 'doc_3',
              title: 'FAQ.text',
              status: 1,
              chunk_count: 18,
              error_message: null,
              source_type: 'text',
              created_at: '2026-01-03T00:00:00Z',
            },
          ],
        },
      ],
    }

    expect(toKnowledgeBaseNodes(data)).toEqual([
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
        name: '工单记录',
        split_strategy: 'size',
        allowed_formats: ['text', 'pdf'],
        chunk_size: 1024,
        documents: [
          {
            document_id: 'doc_2',
            title: '工单.pdf',
            status: 2,
            chunk_count: 0,
            error_message: '解析失败：正文为空',
            source_type: 'pdf',
            created_at: '2026-01-02T00:00:00Z',
          },
          {
            document_id: 'doc_3',
            title: 'FAQ.text',
            status: 1,
            chunk_count: 18,
            error_message: null,
            source_type: 'text',
            created_at: '2026-01-03T00:00:00Z',
          },
        ],
      },
    ])
  })

  it('字段缺省：数组与 error_message 缺失时取安全默认，不让页面拿到 undefined', () => {
    // 后端契约里这些字段恒在；这里模拟的是旧缓存 / 半截负载。
    const data: ConsoleTreeData = {
      knowledge_bases: [
        {
          kb_id: 'kb_old',
          name: '旧库',
          split_strategy: 'size',
          chunk_size: 512,
          documents: [
            {
              document_id: 'doc_old',
              title: '老文档',
              status: 1,
              chunk_count: 3,
              source_type: 'text',
              created_at: '2026-02-01T00:00:00Z',
            },
          ],
        },
      ],
    }

    expect(toKnowledgeBaseNodes(data)).toEqual([
      {
        kb_id: 'kb_old',
        name: '旧库',
        split_strategy: 'size',
        allowed_formats: [],
        chunk_size: 512,
        documents: [
          {
            document_id: 'doc_old',
            title: '老文档',
            status: 1,
            chunk_count: 3,
            error_message: null,
            source_type: 'text',
            created_at: '2026-02-01T00:00:00Z',
          },
        ],
      },
    ])
  })
})

describe('fetchConsoleTree', () => {
  it('只打一次 GET /console/knowledge-bases/tree，不带查询参数', async () => {
    const payload: ConsoleTreeData = { knowledge_bases: [] }
    mocks.get.mockResolvedValue(payload)

    await expect(fetchConsoleTree()).resolves.toEqual(payload)
    expect(mocks.get).toHaveBeenCalledTimes(1)
    // 只传 URL：树是按会话租户取全量，没有任何可传给后端的过滤参数。
    expect(mocks.get.mock.calls[0]).toEqual(['/console/knowledge-bases/tree'])
  })
})
