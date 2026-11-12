import { describe, expect, it } from 'vitest'

import { renderMarkdown } from '../markdown'

/**
 * 报告渲染的四条底线：
 *
 * 1. **原始 HTML 一律转义**（`html: false`）——`v-html` 的唯一安全前提；
 * 2. 结构要真的渲染出来（标题 / 表格 / 列表），而不是原样吐一段文本；
 * 3. 样式靠注入的 Tailwind 类名，不靠组件里的 CSS（仓库里没有 `<style>` 块）；
 * 4. 空输入回空串，不回 `"null"` 字面量。
 */
describe('renderMarkdown：报告正文', () => {
  it('把标题渲染成真正的标题元素，并带上类名', () => {
    const html = renderMarkdown('## Scope')

    expect(html).toContain('<h2')
    expect(html).toContain('Scope')
    expect(html).toMatch(/<h2 class="[^"]*font-semibold/)
  })

  it('把表格渲染成 table，而不是一段竖线文本', () => {
    const html = renderMarkdown('| 档位 | CP |\n| --- | --- |\n| speed | 0.5 |')

    expect(html).toContain('<table')
    expect(html).toContain('<th')
    expect(html).toContain('<td')
    expect(html).toContain('0.5')
  })

  it('列表渲染成 ul/li', () => {
    const html = renderMarkdown('- 第一项\n- 第二项')

    expect(html).toContain('<ul')
    expect(html).toContain('<li')
    expect(html).toContain('第二项')
  })

  it('原始 HTML 被转义：不可能通过报告注入 script', () => {
    const html = renderMarkdown('<script>alert(1)</script>')

    // 关键断言：输出里不能出现真正的标签。
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })

  it('行内代码与代码块保留等宽，且内容被正确转义', () => {
    const inline = renderMarkdown('用 `AUTH_ENABLED=true` 试一下')
    const fence = renderMarkdown('```powershell\nuv run pytest <x>\n```')

    expect(inline).toContain('<code')
    expect(inline).toContain('AUTH_ENABLED=true')
    expect(fence).toContain('<pre')
    expect(fence).toContain('uv run pytest')
    // 代码块里的尖括号同样要转义。
    expect(fence).toContain('&lt;x&gt;')
  })

  it('外链在新标签打开，并断掉 opener', () => {
    const html = renderMarkdown('[报告](https://example.com/x)')

    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('空输入回空串，不回 "null" 字面量', () => {
    expect(renderMarkdown('')).toBe('')
    expect(renderMarkdown(null)).toBe('')
    expect(renderMarkdown(undefined)).toBe('')
  })
})
