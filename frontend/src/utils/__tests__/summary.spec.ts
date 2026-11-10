import { describe, expect, it } from 'vitest'

import { buildRunSummary } from '../format'
import type { RetrievalMetadata } from '../../types/retrieval'

/**
 * 运行摘要的验收是「每个标签都来自响应 metadata，不看页面上的配置摘要」（issue #87）。
 *
 * 把这件事做成纯函数，就可以用一条测试钉住每个值来自哪条字段路径：
 * 页面只负责把 rows 摆出来，不再自己从 query / 勾选状态里拼任何值。
 * 期望值全部是测试里手写的字面量，与被测函数无关。
 */
function valueOf(rows: ReturnType<typeof buildRunSummary>, field: string): string {
  const row = rows.find((item) => item.field === field)
  if (row === undefined) throw new Error(`摘要里没有 ${field} 这一行`)
  return row.value
}

const METADATA: RetrievalMetadata = {
  mode: 'hybrid',
  top_k: 5,
  vector_store: 'pgvector',
  retrieval: {
    mode: 'hybrid',
    degraded: true,
    degraded_reason: 'vector search failed',
    empty_reason: null,
    rerank: null,
  },
  tenant_policy: {
    plan: 'quality',
    requested_profile: 'speed',
    mode: 'hybrid',
    top_k: 5,
    rerank: false,
    rewrite: false,
    kb_count: 2,
    clipped: [],
  },
}

describe('buildRunSummary', () => {
  it('根字段与 retrieval 子字段逐条取自响应，英文码原样', () => {
    const rows = buildRunSummary(METADATA)

    expect(valueOf(rows, 'mode')).toBe('hybrid')
    expect(valueOf(rows, 'top_k')).toBe('5')
    expect(valueOf(rows, 'vector_store')).toBe('pgvector')
    expect(valueOf(rows, 'retrieval.degraded')).toBe('true')
    // 英文码不做中文替身；中文解释只进 tooltip。
    expect(valueOf(rows, 'retrieval.degraded_reason')).toBe('vector search failed')
    expect(rows.find((row) => row.field === 'retrieval.degraded_reason')?.hint).toContain(
      '向量',
    )
    // 有结果时 empty_reason 是 null，显示「—」而不是 0 / 空串。
    expect(valueOf(rows, 'retrieval.empty_reason')).toBe('—')
  })

  it('tenant_policy 存在时逐项展示，值全部来自响应', () => {
    const rows = buildRunSummary(METADATA)

    expect(valueOf(rows, 'tenant_policy.plan')).toBe('quality')
    expect(valueOf(rows, 'tenant_policy.requested_profile')).toBe('speed')
    expect(valueOf(rows, 'tenant_policy.rerank')).toBe('false')
    expect(valueOf(rows, 'tenant_policy.rewrite')).toBe('false')
    expect(valueOf(rows, 'tenant_policy.kb_count')).toBe('2')
    expect(valueOf(rows, 'tenant_policy.clipped')).toBe('—')
  })

  it('响应没有 tenant_policy 时不显示那一栏', () => {
    const rows = buildRunSummary({ ...METADATA, tenant_policy: undefined })

    expect(rows.some((row) => row.field.startsWith('tenant_policy.'))).toBe(false)
  })

  it('四个降级码各有一句中文解释，码本身不改写', () => {
    const codes = [
      'vector search failed',
      'keyword search failed',
      'rerank failed',
      'query rewrite failed',
    ]
    for (const code of codes) {
      const rows = buildRunSummary({
        ...METADATA,
        retrieval: { ...METADATA.retrieval, degraded: true, degraded_reason: code },
      })
      expect(valueOf(rows, 'retrieval.degraded_reason')).toBe(code)
      expect(rows.find((row) => row.field === 'retrieval.degraded_reason')?.hint).not.toBeNull()
    }
  })
})
