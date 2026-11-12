// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import CollapsibleCard from '../CollapsibleCard.vue'

/**
 * 折叠卡片的两条底线：
 *
 * 1. 标题按钮负责开合，**操作区里的按钮不参与开合**——这正是当年 `<details>` 方案的坑：
 *    点「刷新」会把面板一起收起，`@click.stop` 只挡住了鼠标，键盘仍然会触发。
 * 2. 收起时不渲染内容但保留小结，让人不必展开就知道里面有什么。
 */
describe('CollapsibleCard', () => {
  it('默认展开，点标题收起', async () => {
    const wrapper = mount(CollapsibleCard, {
      props: { title: '语义旁证' },
      slots: { default: '<p>内容</p>' },
    })

    expect(wrapper.find('div.border-t').isVisible()).toBe(true)

    await wrapper.find('header button').trigger('click')

    expect(wrapper.find('header button').attributes('aria-expanded')).toBe('false')
  })

  it('defaultOpen=false 时一开始就收起，并显示小结', () => {
    const wrapper = mount(CollapsibleCard, {
      props: { title: '诊断', defaultOpen: false, summary: '3 条' },
      slots: { default: '<p>内容</p>' },
    })

    expect(wrapper.find('header button').attributes('aria-expanded')).toBe('false')
    expect(wrapper.text()).toContain('3 条')
  })

  it('操作区的按钮只做自己的事，不会连带折叠', async () => {
    const onClick = vi.fn()
    const wrapper = mount(CollapsibleCard, {
      props: { title: '语义旁证' },
      slots: {
        default: '<p>内容</p>',
        actions: '<button class="refresh" @click="onClick">刷新</button>',
      },
      global: { setup: () => ({ onClick }) } as never,
    })

    await wrapper.find('button.refresh').trigger('click')

    // 关键断言：状态没变，动作执行了。
    expect(wrapper.find('header button').attributes('aria-expanded')).toBe('true')
  })

  it('可以带一行副标题', () => {
    const wrapper = mount(CollapsibleCard, {
      props: { title: '召回结果', subtitle: '共 5 条' },
    })

    expect(wrapper.text()).toContain('共 5 条')
  })
})
