// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { DOCUMENT_STATUS } from '../../utils/documentStatus'
import StatusBadge from '../StatusBadge.vue'

/**
 * 四态徽章：`status` → 文案 / 颜色的唯一入口。
 *
 * 要守的两件事：
 *
 * 1. **四态各有各的说法**（成功 / 失败 / 索引中 / 排队中），未知值不猜——新增状态时
 *    宁可显示「未知」也不要静默显示成绿色；
 * 2. 颜色**只走 `tokens.css` 的变量**（`rgb(var(--c-*))`），不写死色值：这套界面有深色
 *    主题，写死浅底会在深色页上变成一块贴纸。排队中与索引中必须是两个不同的变量，
 *    否则「在等」和「在跑」在界面上又分不出来了。
 */

const STUBS = {
  // 徽章本身不依赖 Element Plus 的样式，只需要外壳能把默认插槽渲出来。
  'el-popover': { template: '<div><slot name="reference" /><slot /></div>' },
}

function badgeOf(status: number) {
  return mount(StatusBadge, { props: { status }, global: { stubs: STUBS } })
}

describe('StatusBadge：四态', () => {
  it('四态各有自己的文案', () => {
    expect(badgeOf(DOCUMENT_STATUS.SUCCESS).text()).toBe('成功')
    expect(badgeOf(DOCUMENT_STATUS.FAILED).text()).toBe('失败')
    expect(badgeOf(DOCUMENT_STATUS.PROCESSING).text()).toBe('索引中')
    expect(badgeOf(DOCUMENT_STATUS.QUEUED).text()).toBe('排队中')
  })

  it('未知状态不猜（5 和 0 都显示「未知」）', () => {
    expect(badgeOf(5).text()).toBe('未知')
    expect(badgeOf(0).text()).toBe('未知')
  })

  it('排队中与索引中用的是两个不同的令牌，且都只读 CSS 变量', () => {
    const indexing = badgeOf(DOCUMENT_STATUS.PROCESSING).find('span').attributes('style')
    const queued = badgeOf(DOCUMENT_STATUS.QUEUED).find('span').attributes('style')

    expect(queued).toContain('var(--c-queued)')
    expect(queued).toContain('var(--c-queued-bg)')
    expect(queued).not.toBe(indexing)
    // 不写死色值：出现 #rrggbb 就说明有人绕过了 tokens.css。
    expect(queued).not.toMatch(/#[0-9a-fA-F]{3,6}/)
  })

  it('失败且带原因时可展开，成功行不给详情入口', () => {
    const failed = mount(StatusBadge, {
      props: { status: DOCUMENT_STATUS.FAILED, errorMessage: '解析失败' },
      global: { stubs: STUBS },
    })
    expect(failed.text()).toContain('详情')
    expect(failed.text()).toContain('解析失败')

    // 成功行可能残留上一轮的文案：给它一个「详情」只会让人以为成功里藏着问题。
    const success = mount(StatusBadge, {
      props: { status: DOCUMENT_STATUS.SUCCESS, errorMessage: '上一轮的失败原因' },
      global: { stubs: STUBS },
    })
    expect(success.text()).not.toContain('详情')
  })
})
