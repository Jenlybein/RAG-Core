// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ResponseFields from '../ResponseFields.vue'

/**
 * 完整字段树的渲染测。
 *
 * 这一票的验收口径是「各个模式返回的全部字段都要展示」，所以这里钉的不是某几个字段名，
 * 而是**一条不漏**这个性质本身：
 *
 * - 嵌套任意深度都能摊到（四个端点的 `metadata` 分组各不相同，深度不固定）；
 * - 前端**没有为任何字段名写分支**——用一棵它从没见过的树来证明这一点，将来后端加分
 *   组不需要改这个组件；
 * - `null` 显示成 `null`（「字段存在且为空」），空容器显示 `{}` / `[]`（「是空的」而不是
 *   「没有这个字段」）——两种语义在这一页必须区分得开。
 */

function textOf(value: unknown): string {
  return mount(ResponseFields, { props: { value } }).text()
}

describe('ResponseFields：任意响应结构都不漏字段', () => {
  it('把四类端点会出现的分组全部摊开（含从没见过的层级）', () => {
    const text = textOf({
      query: '退款多久到账',
      metadata: {
        trace_id: 'abc',
        retrieval: {
          mode: 'hybrid',
          degraded: false,
          rerank: { applied: true, candidates: 20 },
          query_optimization: { rewrite_applied: true, vector_queries: ['原句', '改写句'] },
          future_group: { depth_two: { depth_three: '要能看到这里' } },
        },
      },
    })

    for (const key of [
      'query',
      'trace_id',
      'retrieval',
      'degraded',
      'rerank',
      'candidates',
      'query_optimization',
      'vector_queries',
      'future_group',
      'depth_three',
      '要能看到这里',
    ]) {
      expect(text).toContain(key)
    }
  })

  it('null 显示成 null，空容器显示 {} / []，不折成「—」也不省略', () => {
    const text = textOf({ a: null, b: {}, c: [], d: '' })

    expect(text).toContain('null')
    expect(text).toContain('{}')
    expect(text).toContain('[]')
    // 空串显示成 ""：否则看起来像「这个字段没有值」。
    expect(text).toContain('""')
  })

  it('标量与布尔原样输出：英文码照原文，不做中文替身', () => {
    const text = textOf({ mode: 'hybrid', applied: true, count: 0 })

    expect(text).toContain('hybrid')
    expect(text).toContain('true')
    // 0 要显示出来：它和「没有这个字段」是两件事。
    expect(text).toContain('0')
  })

  it('数组逐项带下标，每一项的字段都能摊到', () => {
    const text = textOf({
      points: [
        { point_id: 'A1', text: '第一个要点' },
        { point_id: 'A2', text: '第二个要点' },
      ],
    })

    expect(text).toContain('[0]')
    expect(text).toContain('[1]')
    expect(text).toContain('A2')
    expect(text).toContain('第二个要点')
  })
})
