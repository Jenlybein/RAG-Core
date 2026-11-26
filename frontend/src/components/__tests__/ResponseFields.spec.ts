// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ResponseFields from '../ResponseFields.vue'

/**
 * 可折叠 JSON 字段树的渲染测。
 *
 * 这一票的验收口径是「各个模式返回的全部字段都要展示」，所以这里钉的不是某几个字段名，
 * 而是**一条不漏**这个性质本身：一棵组件从没见过的树（含它没有为任何字段名写分支）
 * 也必须能逐层点开看到叶子。
 *
 * 四条边界值得单独钉：
 *
 * 1. **默认折叠**：容器默认只摊到 `defaultOpenDepth`，不能一进页面就把这一栏撑成几屏；
 * 2. **点开能看到叶子**：折叠不是「看不见」，只是「要点一下」；
 * 3. **「全部展开」广播到每一层**：这是父级那个按钮唯一的作用，漏一层就等于没实现；
 * 4. `null` 显示成 `null`、空容器显示 `{}` / `[]`、空串显示 `""`——这三种都是「有值」，
 *    不能折成「—」或省略，否则「字段存在且为空」与「没这个字段」就分不开了。
 */

function textOf(value: unknown, extraProps: Record<string, unknown> = {}): string {
  return mount(ResponseFields, { props: { value, ...extraProps } }).text()
}

const SAMPLE = {
  query: '退款多久到账',
  metadata: {
    trace_id: 'abc',
    retrieval: {
      mode: 'hybrid',
      degraded: false,
      rerank: { applied: true, candidates: 20 },
      query_optimization: { rewrite_applied: true, recall_queries: ['原句', '改写句'] },
      future_group: { depth_two: { depth_three: '要能看到这里' } },
    },
  },
}

describe('ResponseFields：任意响应结构都不漏字段', () => {
  it('全部展开时把四类端点会出现的分组都摊到叶子（含从没见过的层级）', () => {
    // 深度给足：等价于点开了每一层。
    const text = textOf(SAMPLE, { defaultOpenDepth: 99 })

    for (const key of [
      'query',
      'trace_id',
      'retrieval',
      'degraded',
      'rerank',
      'candidates',
      'query_optimization',
      'recall_queries',
      'future_group',
      'depth_three',
      '要能看到这里',
    ]) {
      expect(text).toContain(key)
    }
  })

  it('默认折叠：深层字段不渲染，但标头报出项数（「这里有几个键」要能看见）', () => {
    const text = textOf(SAMPLE, { defaultOpenDepth: 1 })

    // 顶层是一行键 + 项数；叶子此刻不该出现。
    expect(text).toContain('metadata')
    expect(text).toContain('{2 项}')
    expect(text).not.toContain('要能看到这里')
  })

  it('点开标头才展开该层，子层仍按默认深度折叠', async () => {
    const wrapper = mount(ResponseFields, { props: { value: SAMPLE, defaultOpenDepth: 1 } })

    // 根节点默认是展开的（收起它只会得到一片空白），所以要点的第一个**带名字的**标头
    // 是 `metadata`。测试点错位置会把这个用例变成「根能不能折叠」——那是另一个问题。
    const header = wrapper
      .findAll('button')
      .find((node) => node.text().includes('metadata'))
    expect(header).toBeDefined()
    await header!.trigger('click')

    const text = wrapper.text()

    // 点开 metadata 后它的直接字段出现，更深一层（retrieval 之下）仍然折叠。
    expect(text).toContain('trace_id')
    expect(text).not.toContain('要能看到这里')
  })

  it('「全部展开」的广播传到每一层：serial 一变就对齐', async () => {
    const wrapper = mount(ResponseFields, {
      props: { value: SAMPLE, defaultOpenDepth: 1, expandAll: null, serial: 0 },
    })

    await wrapper.setProps({ expandAll: 'open', serial: 1 })
    expect(wrapper.text()).toContain('要能看到这里')

    await wrapper.setProps({ expandAll: 'closed', serial: 2 })
    expect(wrapper.text()).not.toContain('要能看到这里')
  })

  it('null 显示成 null，空容器显示 {} / []，空串显示 ""', () => {
    const text = textOf({ a: null, b: {}, c: [], d: '' }, { defaultOpenDepth: 99 })

    expect(text).toContain('null')
    expect(text).toContain('{}')
    expect(text).toContain('[]')
    expect(text).toContain('""')
  })

  it('标量与布尔原样输出：英文码照原文，0 也算有值', () => {
    const text = textOf({ mode: 'hybrid', applied: true, count: 0 }, { defaultOpenDepth: 99 })

    expect(text).toContain('hybrid')
    expect(text).toContain('true')
    expect(text).toContain('0')
  })

  it('数组逐项带下标，每一项的字段都能摊到', () => {
    const text = textOf(
      {
        points: [
          { point_id: 'A1', text: '第一个要点' },
          { point_id: 'A2', text: '第二个要点' },
        ],
      },
      { defaultOpenDepth: 99 },
    )

    expect(text).toContain('[0]')
    expect(text).toContain('[1]')
    expect(text).toContain('A2')
    expect(text).toContain('第二个要点')
  })
})
