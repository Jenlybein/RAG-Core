// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import RunSummary from '../RunSummary.vue'
import type { RetrievalMetadata } from '../../types/retrieval'

/**
 * 运行摘要的渲染测（spec 的 Testing Decisions 明确要求：摘要组件测，mock 网络）。
 *
 * 这里不挂真实后端、也不 import 任何服务：组件只收 `metadata` 一个 prop，所以「摘要不
 * 看页面输入」在结构上已经成立，本用例要钉的是渲染层的那几条约定：
 *
 * - 四个英文码（mode / vector_store / degraded_reason / empty_reason）**照原文**显示，
 *   不做中文替身——否则拿着译文去搜日志会搜不到；
 * - 缺省 / 空集合显示「—」，不能折成 0 或空串；
 * - 降级横幅要同时给出 `degraded = true` 与原因原文。
 *
 * 全局 `vitest.config.ts` 的 environment 仍是 node（别让它牵连纯函数用例），
 * 本文件用首行的 `@vitest-environment jsdom` 单独声明。
 */

function metadataOf(): RetrievalMetadata {
  return {
    mode: 'hybrid',
    top_k: 5,
    vector_store: 'pgvector',
    retrieval: {
      mode: 'hybrid',
      degraded: true,
      degraded_reason: 'rerank failed',
      empty_reason: 'no_chunks_matched',
      rerank: null,
    },
    tenant_policy: {
      plan: 'pro',
      requested_profile: 'quality',
      mode: 'hybrid',
      top_k: 5,
      rerank: false,
      rewrite: true,
      kb_count: 1,
      // 空集合 = 没被套餐裁掉任何字段，界面显示「—」而不是空串。
      clipped: [],
    },
  }
}

// 只桩掉 tooltip：它的交互不是本用例的重点，但默认插槽必须照常渲染出值文本。
const stubs = {
  components: {
    ElTooltip: { template: '<span><slot /></span>' },
  },
}

describe('RunSummary：英文码照原文、缺省显示「—」', () => {
  it('渲染出四个英文码原文与「—」占位', () => {
    const wrapper = mount(RunSummary, {
      props: { metadata: metadataOf() },
      global: stubs,
    })

    const text = wrapper.text()
    // 四个英文码：检索模式、向量库、降级原因、空结果原因，全部照抄原文。
    expect(text).toContain('hybrid')
    expect(text).toContain('pgvector')
    expect(text).toContain('rerank failed')
    expect(text).toContain('no_chunks_matched')
    // 空集合（clipped = []）显示「—」，不是空串。
    expect(text).toContain('—')
  })

  it('降级时横幅写明 degraded 与原因原文', () => {
    const wrapper = mount(RunSummary, {
      props: { metadata: metadataOf() },
      global: stubs,
    })

    expect(wrapper.text()).toContain('degraded = true')
    expect(wrapper.text()).toContain('rerank failed')
  })

  it('响应没有 tenant_policy 时如实说明，而不是渲染一栏空值', () => {
    const withoutPolicy = metadataOf()
    delete withoutPolicy.tenant_policy

    const wrapper = mount(RunSummary, {
      props: { metadata: withoutPolicy },
      global: stubs,
    })

    expect(wrapper.text()).toContain('这次响应没有 tenant_policy')
    expect(wrapper.text()).not.toContain('要求的档位')
  })
})
