import { describe, expect, it } from 'vitest'

import type { DocumentListItem } from '../../types/document'
import type { KnowledgeBaseNode } from '../../types/knowledgeBase'
import { shouldContinuePolling } from '../useTreePolling'

/**
 * 轮询停止条件是列表页唯一需要「每 3 秒」的判定，也是最容易在重构里被改坏的一行：
 * 它必须同时满足「有在途文档就继续」与「全部终态就停」。
 *
 * 四态之后「在途」是**排队中（4）或索引中（3）**两个状态，不是「等于 3」：上传之后
 * 后端一律回排队中，只认 3 会让刚上传的文档永远停在「排队中」不再刷新——而它可能早就
 * 跑完了。这里不 mock 定时器，只钉返回值。
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
    section_part_max_chars: 2000,
    needs_rebuild: false,
    documents,
  }
}

describe('shouldContinuePolling', () => {
  it('有文档在索引中（3）时返回 3000', () => {
    const tree = [knowledgeBaseWith('a', [documentWith(3)])]
    expect(shouldContinuePolling(tree)).toBe(3000)
  })

  it('有文档在排队中（4）时同样返回 3000', () => {
    // 这是本条规则在四态里最容易漏的一支：上传回执就是 4，漏了它整页不再刷新。
    const tree = [knowledgeBaseWith('a', [documentWith(4)])]
    expect(shouldContinuePolling(tree)).toBe(3000)
  })

  it('只有失败（2）这种终态时不轮询', () => {
    // 失败不会自己变好：继续 3 秒一轮只是白刷。
    const tree = [knowledgeBaseWith('a', [documentWith(1), documentWith(2)])]
    expect(shouldContinuePolling(tree)).toBe(false)
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
    expect(shouldContinuePolling(null)).toBe(false)
    expect(shouldContinuePolling(undefined)).toBe(false)
  })

  it('混合终态里只要还有一个在途文档，就继续轮询', () => {
    const tree = [
      knowledgeBaseWith('a', [documentWith(1), documentWith(2)]),
      knowledgeBaseWith('b', [documentWith(3), documentWith(1)]),
    ]
    expect(shouldContinuePolling(tree)).toBe(3000)

    const withQueued = [
      knowledgeBaseWith('a', [documentWith(1), documentWith(2)]),
      knowledgeBaseWith('b', [documentWith(4), documentWith(1)]),
    ]
    expect(shouldContinuePolling(withQueued)).toBe(3000)
  })
})
