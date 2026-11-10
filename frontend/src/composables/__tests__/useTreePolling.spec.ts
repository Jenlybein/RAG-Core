import { describe, expect, it } from 'vitest'

import type { DocumentListItem } from '../../types/document'
import type { KnowledgeBaseNode } from '../../types/knowledgeBase'
import { shouldContinuePolling } from '../useTreePolling'

/**
 * 轮询停止条件是列表页唯一需要「每 3 秒」的判定，也是最容易在重构里被改坏的一行：
 * 它必须同时满足「有处理中就继续」与「全部终态就停」。这里不 mock 定时器，
 * 只钉返回值——`useQuery` 拿到什么间隔由它决定，测试不需要知道那是怎么排期的。
 */

function documentWith(status: number): DocumentListItem {
  return {
    document_id: `doc-${status}`,
    title: `文档 ${status}`,
    status,
    chunk_count: 0,
    error_message: null,
    source_type: 'markdown',
    created_at: '2026-01-01T00:00:00Z',
  }
}

function knowledgeBaseWith(name: string, documents: DocumentListItem[]): KnowledgeBaseNode {
  return {
    kb_id: `kb-${name}`,
    name,
    split_strategy: 'structure',
    allowed_formats: ['markdown'],
    chunk_size: 512,
    documents,
  }
}

describe('shouldContinuePolling', () => {
  it('有文档还在索引中时返回 3000', () => {
    const tree = [knowledgeBaseWith('a', [documentWith(3)])]
    expect(shouldContinuePolling(tree)).toBe(3000)
  })

  it('全部文档进入终态时返回 false（请求停下）', () => {
    const tree = [
      knowledgeBaseWith('a', [documentWith(1), documentWith(2)]),
      knowledgeBaseWith('b', [documentWith(1)]),
    ]
    expect(shouldContinuePolling(tree)).toBe(false)
  })

  it('空树（还没建库或所有库都没有文档）时返回 false', () => {
    expect(shouldContinuePolling([])).toBe(false)
    expect(shouldContinuePolling([knowledgeBaseWith('empty', [])])).toBe(false)
  })

  it('混合终态里只要还有一个处理中，就继续轮询', () => {
    const tree = [
      knowledgeBaseWith('a', [documentWith(1), documentWith(2)]),
      knowledgeBaseWith('b', [documentWith(3), documentWith(1)]),
    ]
    expect(shouldContinuePolling(tree)).toBe(3000)
  })
})
