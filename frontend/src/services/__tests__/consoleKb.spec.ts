import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}))

// 只替换「网络那一层」：这里测的是服务模块对控制面契约的承诺（路径、次数、形状），
// 不是 axios 的行为。
vi.mock('../../lib/consoleHttp', () => ({ consoleHttp: mocks }))

import type { ConsoleTreeData } from '../../types/knowledgeBase'
import {
  createKbConsole,
  deleteDocumentConsole,
  fetchConsoleTree,
  reindexDocumentConsole,
  toKnowledgeBaseNodes,
  uploadConsole,
} from '../consoleKb'

beforeEach(() => {
  mocks.get.mockReset()
  mocks.post.mockReset()
  mocks.delete.mockReset()
})

/**
 * 映射是「页面渲不渲得出来」的最后一道门：`KnowledgeManagePage` 会直接读
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

/**
 * 本票 (#92) 把建库、上传、删文档、重建这批写操作从数据面（Bearer dev-key）改走
 * 控制面会话门。这里钉死的是**门**换了、**契约**没换：路径与请求体仍照
 * `app/api/routes/console_kbs.py`，响应形状与数据面逐字段相同。
 */
describe('createKbConsole', () => {
  it('POST /console/knowledge-bases，请求体原样是建库请求', async () => {
    const receipt = {
      kb_id: 'kb_1',
      name: '产品手册',
      tenant_id: 'tenant_1',
      split_strategy: 'structure',
      allowed_formats: ['markdown'],
      chunk_size: 512,
      created_at: '2026-01-01T00:00:00Z',
    }
    mocks.post.mockResolvedValue(receipt)
    const payload = {
      name: '产品手册',
      split_strategy: 'structure' as const,
      allowed_formats: ['markdown' as const],
      chunk_size: 512,
    }

    await expect(createKbConsole(payload)).resolves.toEqual(receipt)
    expect(mocks.post).toHaveBeenCalledTimes(1)
    // 期望值写死字面量：`extra=forbid`，多一个字段就是 20002。
    expect(mocks.post.mock.calls[0]).toEqual(['/console/knowledge-bases', payload])
  })
})

describe('uploadConsole', () => {
  const receipt = {
    document_id: 'doc_1',
    kb_id: 'kb_1',
    status: 3,
    chunk_count: 0,
    error_message: null,
    source_type: 'markdown',
  }

  it('JSON 入口：kb_id 在路径里，正文只发 title + content', async () => {
    mocks.post.mockResolvedValue(receipt)

    await expect(
      uploadConsole({ kb_id: 'kb_1', title: '退款政策.md', content: '# 退款' }),
    ).resolves.toEqual(receipt)

    expect(mocks.post).toHaveBeenCalledTimes(1)
    expect(mocks.post.mock.calls[0]).toEqual([
      '/console/knowledge-bases/kb_1/documents',
      { title: '退款政策.md', content: '# 退款' },
    ])
  })

  it('multipart 入口：FormData 里是 title + file，kb_id 同样只在路径里', async () => {
    mocks.post.mockResolvedValue(receipt)
    const file = new File(['# 退款'], '退款政策.md', { type: 'text/markdown' })

    await uploadConsole({ kb_id: 'kb_1', title: '退款政策.md', file })

    const [url, body] = mocks.post.mock.calls[0]
    expect(url).toBe('/console/knowledge-bases/kb_1/documents')
    expect(body).toBeInstanceOf(FormData)
    expect((body as FormData).get('title')).toBe('退款政策.md')
    expect((body as FormData).get('file')).toBeInstanceOf(File)
    // 不给第三个参数（更不手写 Content-Type）：没有 boundary 的 multipart 会被 20002。
    expect(mocks.post.mock.calls[0]).toHaveLength(2)
  })
})

describe('deleteDocumentConsole', () => {
  it('DELETE /console/documents/{document_id}', async () => {
    const receipt = { document_id: 'doc_1', deleted: true }
    mocks.delete.mockResolvedValue(receipt)

    await expect(deleteDocumentConsole('doc_1')).resolves.toEqual(receipt)
    expect(mocks.delete).toHaveBeenCalledWith('/console/documents/doc_1')
  })
})

describe('reindexDocumentConsole', () => {
  it('POST /console/documents/{document_id}/reindex，两个查询参数默认 false', async () => {
    const receipt = { document_id: 'doc_1', status: 3, chunk_count: 0, reparse: false }
    mocks.post.mockResolvedValue(receipt)

    await expect(reindexDocumentConsole('doc_1')).resolves.toEqual(receipt)

    const [url, body, config] = mocks.post.mock.calls[0]
    expect(url).toBe('/console/documents/doc_1/reindex')
    // 重建没有请求体：两个开关都在 query 上。
    expect(body).toBeUndefined()
    expect(config).toEqual({ params: { reparse: false, force: false } })
  })

  it('显式要求 reparse / force 时如实带上', async () => {
    mocks.post.mockResolvedValue({ document_id: 'doc_1', status: 3, chunk_count: 0, reparse: true })

    await reindexDocumentConsole('doc_1', { reparse: true, force: true })

    expect(mocks.post.mock.calls[0][2]).toEqual({ params: { reparse: true, force: true } })
  })
})
