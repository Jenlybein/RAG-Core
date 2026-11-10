import { describe, expect, it } from 'vitest'

import {
  diffResults,
  diffSummary,
  pushHistory,
  type CompareHistoryEntry,
} from '../retrieveCompare'
import type {
  RetrievalMetadata,
  RetrievedChunk,
  RetrieveData,
  TenantPolicy,
} from '../../types/retrieval'

/**
 * 对比面板的三条产品规则：会话内最多留 5 次、摘要比对全部字段、结果按 `(kb_id, chunk_id)`
 * 配对且 `null` 与 `0` 不是一回事（00-facts §5：未命中的那一路是 null，不是 0）。
 *
 * 期望值全部写死成字面量，不从被测函数再算一遍：这些字符串就是面板上要出现的东西。
 * 这里不挂 Vue：panel 只负责把 rows 摆出来。
 */

function policyOf(overrides: Partial<TenantPolicy> = {}): TenantPolicy {
  return {
    plan: 'free',
    requested_profile: 'speed',
    mode: 'vector',
    top_k: 5,
    rerank: false,
    rewrite: false,
    kb_count: 1,
    clipped: [],
    ...overrides,
  }
}

function metadataOf(overrides: Partial<RetrievalMetadata> = {}): RetrievalMetadata {
  return {
    mode: 'vector',
    top_k: 5,
    vector_store: 'pgvector',
    retrieval: {
      mode: 'vector',
      degraded: false,
      degraded_reason: null,
      empty_reason: null,
      rerank: null,
    },
    tenant_policy: policyOf(),
    ...overrides,
  }
}

function chunkOf(overrides: Partial<RetrievedChunk> = {}): RetrievedChunk {
  return {
    document_id: 'doc-1',
    chunk_id: 'chunk-1',
    title: '退款到账时间',
    content: '一般 3 个工作日到账',
    score: 0.5,
    kb_id: 'kb-1',
    vector_score: 0.42,
    bm25_score: null,
    vector_rank: 1,
    bm25_rank: null,
    rrf_score: 0.03,
    rerank_score: null,
    section_id: null,
    source_chunk_ids: [],
    expansion: null,
    heading_path: null,
    page: null,
    doc_version: null,
    split_strategy: null,
    ...overrides,
  }
}

function dataOf(overrides: Partial<RetrieveData> = {}): RetrieveData {
  return {
    query: '退款多久到账',
    kb_ids: ['kb-1'],
    retrieved_chunks: [],
    metadata: metadataOf(),
    ...overrides,
  }
}

function entryOf(seq: number, data: RetrieveData = dataOf()): CompareHistoryEntry {
  return { seq, at: `2026-11-10T09:00:0${seq}Z`, data }
}

function rowOf(rows: ReturnType<typeof diffSummary>, field: string) {
  const row = rows.find((item) => item.field === field)
  if (row === undefined) throw new Error(`摘要对比里没有 ${field} 这一行`)
  return row
}

describe('pushHistory：内存历史，FIFO 上限 5', () => {
  it('追加到末尾，返回新数组，不动传进来的历史', () => {
    const history = [entryOf(1)]
    const next = pushHistory(history, entryOf(2))

    expect(next.map((item) => item.seq)).toEqual([1, 2])
    // 原地修改会让「上一次」这样的引用在组件之间悄悄变掉；纯函数返回新数组才可预测。
    expect(history.map((item) => item.seq)).toEqual([1])
  })

  it('默认上限 5：第 6 次挤掉最旧的那次，顺序保持', () => {
    let history: CompareHistoryEntry[] = []
    for (let seq = 1; seq <= 6; seq += 1) {
      history = pushHistory(history, entryOf(seq))
    }

    expect(history.map((item) => item.seq)).toEqual([2, 3, 4, 5, 6])
  })

  it('limit 可覆盖：limit=2 时只留最近两次', () => {
    let history: CompareHistoryEntry[] = []
    for (let seq = 1; seq <= 3; seq += 1) {
      history = pushHistory(history, entryOf(seq), 2)
    }

    expect(history.map((item) => item.seq)).toEqual([2, 3])
  })

  it('limit 非正数时返回空数组（等于把对比面板关掉，不该留一条）', () => {
    expect(pushHistory([entryOf(1)], entryOf(2), 0)).toEqual([])
  })
})

describe('diffSummary：运行摘要逐字段比对', () => {
  it('七项全部列出（没变的也在），顺序固定', () => {
    const rows = diffSummary(dataOf(), dataOf())

    expect(rows.map((row) => row.field)).toEqual([
      'mode',
      'top_k',
      'tenant_policy.rerank',
      'tenant_policy.rewrite',
      'tenant_policy.clipped',
      'tenant_policy.plan',
      'tenant_policy.requested_profile',
    ])
    // 没变也要有据可查：只标 changed=false，不删行。
    expect(rows.every((row) => row.changed === false)).toBe(true)
    expect(rowOf(rows, 'mode').before).toBe('vector')
    expect(rowOf(rows, 'mode').after).toBe('vector')
  })

  it('mode / top_k / rerank / rewrite 变了就标出来，值两边都给出', () => {
    const base = dataOf()
    const current = dataOf({
      metadata: metadataOf({
        mode: 'hybrid',
        top_k: 8,
        tenant_policy: policyOf({ mode: 'hybrid', top_k: 8, rerank: true, rewrite: true }),
      }),
    })

    const rows = diffSummary(base, current)

    const changedFields = rows.filter((row) => row.changed).map((row) => row.field)
    expect(changedFields).toEqual([
      'mode',
      'top_k',
      'tenant_policy.rerank',
      'tenant_policy.rewrite',
    ])
    expect(rowOf(rows, 'mode').before).toBe('vector')
    expect(rowOf(rows, 'mode').after).toBe('hybrid')
    expect(rowOf(rows, 'top_k').before).toBe('5')
    expect(rowOf(rows, 'top_k').after).toBe('8')
    expect(rowOf(rows, 'tenant_policy.rerank').before).toBe('false')
    expect(rowOf(rows, 'tenant_policy.rerank').after).toBe('true')
  })

  it('clipped 按集合比：顺序不同、有重复都不算变化', () => {
    const base = dataOf({
      metadata: metadataOf({ tenant_policy: policyOf({ clipped: ['top_k', 'rerank'] }) }),
    })
    const current = dataOf({
      metadata: metadataOf({ tenant_policy: policyOf({ clipped: ['rerank', 'top_k', 'top_k'] }) }),
    })

    const rows = diffSummary(base, current)

    expect(rowOf(rows, 'tenant_policy.clipped').changed).toBe(false)
    expect(rowOf(rows, 'tenant_policy.clipped').before).toBe('rerank、top_k')
  })

  it('clipped 真的少了一项就标变化，并给出两边的集合', () => {
    const base = dataOf({
      metadata: metadataOf({ tenant_policy: policyOf({ clipped: ['top_k', 'rerank'] }) }),
    })
    const current = dataOf({
      metadata: metadataOf({ tenant_policy: policyOf({ clipped: ['rerank'] }) }),
    })

    const row = rowOf(diffSummary(base, current), 'tenant_policy.clipped')
    expect(row.changed).toBe(true)
    expect(row.before).toBe('rerank、top_k')
    expect(row.after).toBe('rerank')
  })

  it('空 clipped 显示「—」，不是空串', () => {
    expect(rowOf(diffSummary(dataOf(), dataOf()), 'tenant_policy.clipped').before).toBe('—')
  })

  it('基准没有 tenant_policy 时，策略行显示「—」并标成变化', () => {
    const base = dataOf({ metadata: metadataOf({ tenant_policy: undefined }) })
    const current = dataOf()

    const rows = diffSummary(base, current)

    expect(rowOf(rows, 'tenant_policy.plan').before).toBe('—')
    expect(rowOf(rows, 'tenant_policy.plan').changed).toBe(true)
    expect(rowOf(rows, 'mode').changed).toBe(false)
  })
})

describe('diffResults：按 (kb_id, chunk_id) 配对', () => {
  it('只在一边出现的切片分别进 added / removed，带上原列表位置与标题', () => {
    const base = dataOf({ retrieved_chunks: [chunkOf({ chunk_id: 'chunk-old' })] })
    const current = dataOf({
      retrieved_chunks: [chunkOf({ chunk_id: 'chunk-new', title: '发票说明', score: 0.7 })],
    })

    const diff = diffResults(base, current)

    expect(diff.removed.map((item) => item.chunk_id)).toEqual(['chunk-old'])
    expect(diff.removed[0].rank).toBe(1)
    expect(diff.added.map((item) => item.chunk_id)).toEqual(['chunk-new'])
    expect(diff.added[0].title).toBe('发票说明')
    expect(diff.added[0].score).toBe(0.7)
    expect(diff.changed).toEqual([])
  })

  it('跨库同 chunk_id 不是同一条：kb_id 不同就是消失 + 新增', () => {
    const base = dataOf({ retrieved_chunks: [chunkOf({ kb_id: 'kb-a' })] })
    const current = dataOf({ retrieved_chunks: [chunkOf({ kb_id: 'kb-b' })] })

    const diff = diffResults(base, current)

    expect(diff.removed).toHaveLength(1)
    expect(diff.added).toHaveLength(1)
    expect(diff.changed).toEqual([])
  })

  it('同一条分数涨了：changed 里 score 标 up，名次没动', () => {
    const base = dataOf({ retrieved_chunks: [chunkOf({ score: 0.5 })] })
    const current = dataOf({ retrieved_chunks: [chunkOf({ score: 0.61 })] })

    const diff = diffResults(base, current)
    expect(diff.changed).toHaveLength(1)
    const score = diff.changed[0].fields.find((field) => field.field === 'score')
    if (score === undefined) throw new Error('changed 里没有 score')

    expect(score.before).toBe(0.5)
    expect(score.after).toBe(0.61)
    expect(score.changed).toBe(true)
    expect(score.direction).toBe('up')
    expect(diff.changed[0].fields.find((field) => field.field === 'rank')?.changed).toBe(false)
  })

  it('null 与 0 不是一回事：未命中 → 第 0 名 算「出现」，0 → 0 才算没变', () => {
    const base = dataOf({ retrieved_chunks: [chunkOf({ vector_rank: null, bm25_rank: 0 })] })
    const current = dataOf({ retrieved_chunks: [chunkOf({ vector_rank: 0, bm25_rank: 0 })] })

    const fields = diffResults(base, current).changed[0].fields
    const vector = fields.find((field) => field.field === 'vector_rank')
    const bm25 = fields.find((field) => field.field === 'bm25_rank')
    if (vector === undefined || bm25 === undefined) throw new Error('changed 里缺名次两路')

    expect(vector.changed).toBe(true)
    expect(vector.before).toBeNull()
    expect(vector.after).toBe(0)
    expect(vector.direction).toBe('appear')
    // 两遍都是 0：真的没变，不能因为 0 容易被当成空值就被判成变化。
    expect(bm25.changed).toBe(false)
    expect(bm25.direction).toBe('none')
  })

  it('名次后退：同一条从第 1 名掉到第 2 名，rank 标 down', () => {
    const a = chunkOf({ chunk_id: 'a', score: 0.6 })
    const b = chunkOf({ chunk_id: 'b', score: 0.9 })
    const base = dataOf({ retrieved_chunks: [a, b] })
    const current = dataOf({ retrieved_chunks: [b, a] })

    const diff = diffResults(base, current)
    const changedA = diff.changed.find((item) => item.chunk_id === 'a')
    const changedB = diff.changed.find((item) => item.chunk_id === 'b')
    if (changedA === undefined || changedB === undefined) throw new Error('两片都应出现在 changed 里')

    const rankA = changedA.fields.find((field) => field.field === 'rank')
    const rankB = changedB.fields.find((field) => field.field === 'rank')
    expect(rankA?.before).toBe(1)
    expect(rankA?.after).toBe(2)
    expect(rankA?.direction).toBe('down')
    expect(rankB?.before).toBe(2)
    expect(rankB?.after).toBe(1)
    expect(rankB?.direction).toBe('up')
  })

  it('完全一致的结果没有 changed 项，只留 unchanged 计数', () => {
    const chunks = [chunkOf({ chunk_id: 'a' }), chunkOf({ chunk_id: 'b' })]
    const diff = diffResults(
      dataOf({ retrieved_chunks: chunks }),
      dataOf({ retrieved_chunks: chunks.map((item) => ({ ...item })) }),
    )

    expect(diff.changed).toEqual([])
    expect(diff.added).toEqual([])
    expect(diff.removed).toEqual([])
    expect(diff.unchanged).toBe(2)
  })
})
