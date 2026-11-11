// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ComparePanel from '../ComparePanel.vue'
import type { RetrievalMetadata, RetrievedChunk, RetrieveData } from '../../types/retrieval'
import type { CompareHistoryEntry } from '../../utils/retrieveCompare'

/**
 * 对比面板的渲染测（spec 的 Testing Decisions 明确要求：对比 diff 的组件测，mock 网络）。
 *
 * 面板不自己算 diff（判定全在 `utils/retrieveCompare.ts` 的纯函数里），所以本用例的
 * 重点是「两列并排」与「变化项有没有标记」这类只有模板才决定的事：
 *
 * - 左右两列分别是「基准 · 第 1 次」与「当前 · 第 2 次」；
 * - 新增 / 消失 / 变化的条数都出现在标题里；
 * - 变化项的方向标记 `↑` / `↓` 必须真的渲染出来（不能只有 `changed` 布尔值上了色）。
 */

function chunkOf(overrides: Partial<RetrievedChunk> = {}): RetrievedChunk {
  return {
    document_id: 'doc-1',
    chunk_id: 'chunk-a',
    title: '默认标题',
    content: '正文',
    score: 0.5,
    kb_id: 'kb-1',
    vector_score: 0.6,
    bm25_score: 0.4,
    vector_rank: 2,
    bm25_rank: 1,
    rrf_score: 0.5,
    rerank_score: null,
    section_id: null,
    source_chunk_ids: [],
    expansion: null,
    heading_path: null,
    page: null,
    doc_version: 1,
    split_strategy: null,
    ...overrides,
  }
}

function metadataOf(): RetrievalMetadata {
  return {
    mode: 'hybrid',
    top_k: 3,
    vector_store: 'pgvector',
    trace_id: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
    retrieval: {
      mode: 'hybrid',
      degraded: false,
      degraded_reason: null,
      empty_reason: null,
      rerank: null,
    },
    tenant_policy: {
      plan: 'pro',
      requested_profile: 'quality',
      mode: 'hybrid',
      top_k: 3,
      rerank: true,
      rewrite: true,
      kb_count: 1,
      clipped: [],
    },
  }
}

function dataOf(chunks: RetrievedChunk[], mode: string): RetrieveData {
  return {
    query: '退款多久到账',
    kb_ids: ['kb-1'],
    retrieved_chunks: chunks,
    metadata: { ...metadataOf(), mode },
  }
}

/** 基准：A（分数 0.5、语义第 2、关键词第 1）+ B（之后会消失）。 */
const baseData = dataOf(
  [
    chunkOf({ chunk_id: 'chunk-a', title: '分数涨了的那条', score: 0.5, vector_rank: 2, bm25_rank: 1, rrf_score: 0.5 }),
    chunkOf({ chunk_id: 'chunk-b', title: '基准独有', score: 0.3, vector_rank: 3, bm25_rank: 2, rrf_score: 0.3 }),
  ],
  'hybrid',
)

/** 当前：A 分数涨、语义名次前移、关键词名次后移；B 消失；C 新增。 */
const currentData = dataOf(
  [
    chunkOf({ chunk_id: 'chunk-a', title: '分数涨了的那条', score: 0.8, vector_rank: 1, bm25_rank: 2, rrf_score: 0.8 }),
    chunkOf({ chunk_id: 'chunk-c', title: '当前新增', score: 0.4, vector_rank: 2, bm25_rank: 3, rrf_score: 0.4 }),
  ],
  'vector',
)

function historyOf(): CompareHistoryEntry[] {
  return [
    { seq: 1, at: '2026-11-10T09:00:00', data: baseData },
    { seq: 2, at: '2026-11-10T09:05:00', data: currentData },
  ]
}

// Element Plus 的卡片 / 按钮 / tooltip 只提供容器与交互，测试里用轻量桩：
// 卡片必须把 header 具名插槽也渲染出来，否则「两列标题」根本不会出现在 DOM 里。
const global = {
  components: {
    ElCard: { template: '<section><slot name="header" /><slot /></section>' },
    ElButton: { template: '<button><slot /></button>' },
    ElTooltip: { template: '<span><slot /></span>' },
  },
}

describe('ComparePanel：两列并排，变化项有标记', () => {
  it('渲染基准 / 当前两列与新增、消失、变化的条数', () => {
    const wrapper = mount(ComparePanel, {
      props: { history: historyOf(), pinnedSeq: null },
      global,
    })

    const text = wrapper.text()
    expect(text).toContain('基准 · 第 1 次')
    expect(text).toContain('当前 · 第 2 次')
    expect(text).toContain('新增 1 条')
    expect(text).toContain('消失 1 条')
    expect(text).toContain('变化 1 条')

    // 两条只在一边出现的结果都点名（标题来自响应，不是占位）。
    expect(text).toContain('基准独有')
    expect(text).toContain('当前新增')
  })

  it('变化项的方向标记真的渲染出来（分数涨是 ↑、名次后移是 ↓）', () => {
    const wrapper = mount(ComparePanel, {
      props: { history: historyOf(), pinnedSeq: null },
      global,
    })

    const text = wrapper.text()
    expect(text).toContain('分数涨了的那条')
    expect(text).toContain('↑')
    expect(text).toContain('↓')
  })
})
