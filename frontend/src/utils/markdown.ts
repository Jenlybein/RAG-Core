import MarkdownIt from 'markdown-it'

/**
 * 旁证报告正文的 Markdown 渲染器。
 *
 * 三条决定，都写在这里免得下次有人「顺手」改掉：
 *
 * 1. **`html: false`**——源里的原始 HTML 一律转义。报告里带题库问句与逐题结论，将来也可能
 *    有人往里写带尖括号的内容；关掉原始 HTML 之后，输出里不可能出现 `<script>`，于是
 *    组件里的 `v-html` 不需要再来一层 sanitizer（少一个依赖，也少一处「忘了调」的机会）。
 *    这条有测试钉着，别为「支持内嵌 HTML」把它打开。
 * 2. **类名在这里注入，而不是在组件里写 CSS**——仓库里没有一个 `<style>` 块，样式全走
 *    Tailwind 工具类。给生成出来的 HTML 打类名，是把这条约定延续下去的唯一办法；
 *    引入 `@tailwindcss/typography` 会让「报告长什么样」多一份不在组件里的真相。
 * 3. **单例**——配置是常量，实例构造有一点开销，而报告弹窗每次打开都会渲染。
 */

const HEADING_CLASSES: Record<string, string> = {
  h1: 'mt-4 mb-2 text-[14px] font-semibold text-slate-800',
  h2: 'mt-4 mb-2 text-[13px] font-semibold text-slate-800',
  h3: 'mt-3 mb-1.5 text-[12px] font-semibold text-slate-700',
  h4: 'mt-3 mb-1 text-[11px] font-semibold text-slate-700',
}

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: false })

markdown.renderer.rules.heading_open = (tokens, index, options, _env, self) => {
  const token = tokens[index]
  const classes = HEADING_CLASSES[token.tag]
  if (classes) token.attrSet('class', classes)
  return self.renderToken(tokens, index, options)
}

/** 段落、列表、表格、引用的类名集中在一次注册里，免得散在十几个 rule 覆写中间。 */
const BLOCK_CLASSES: Record<string, string> = {
  paragraph_open: 'my-1.5 text-[11px] leading-relaxed text-slate-700',
  bullet_list_open: 'my-1.5 list-disc pl-4 text-[11px] leading-relaxed text-slate-700',
  ordered_list_open: 'my-1.5 list-decimal pl-4 text-[11px] leading-relaxed text-slate-700',
  list_item_open: 'my-0.5',
  blockquote_open: 'my-2 border-l-2 border-line pl-3 text-[11px] text-slate-500',
  table_open: 'my-2 w-full border-collapse text-[11px]',
  thead_open: 'bg-slate-50',
  th_open: 'border border-line px-2 py-1 text-left font-semibold text-slate-700',
  td_open: 'border border-line px-2 py-1 align-top text-slate-600',
  hr: 'my-3 border-0 border-t border-line',
}

for (const [rule, classes] of Object.entries(BLOCK_CLASSES)) {
  if (rule === 'hr') continue
  markdown.renderer.rules[rule] = (tokens, index, options, _env, self) => {
    tokens[index].attrSet('class', classes)
    return self.renderToken(tokens, index, options)
  }
}

markdown.renderer.rules.hr = () => '<hr class="my-3 border-0 border-t border-line" />\n'

markdown.renderer.rules.code_inline = (tokens, index) => {
  // 内容由 markdown-it 自己转义过（`html: false`），这里只加类名。
  const escaped = markdown.utils.escapeHtml(tokens[index].content)
  return `<code class="rounded bg-slate-100 px-1 py-0.5 font-mono text-[10px] text-slate-700">${escaped}</code>`
}

markdown.renderer.rules.fence = (tokens, index) => {
  const token = tokens[index]
  // 代码块保留等宽 + 深底：报告里的复现命令读起来才像命令。
  return (
    '<pre class="my-2 overflow-auto rounded-control bg-slate-900 p-2 text-[10px] leading-relaxed text-slate-100">' +
    `<code>${markdown.utils.escapeHtml(token.content)}</code></pre>\n`
  )
}

markdown.renderer.rules.link_open = (tokens, index, options, _env, self) => {
  const token = tokens[index]
  // 导出的报告里可能有外链：新标签打开并断掉 `window.opener`，别让一个链接接管控制台。
  token.attrSet('target', '_blank')
  token.attrSet('rel', 'noopener noreferrer')
  token.attrSet('class', 'text-brand underline')
  return self.renderToken(tokens, index, options)
}

/**
 * 把报告正文渲染成 HTML。
 *
 * 空串 / `null` 一律回 `''`：调用方拿到空串就渲染一个空容器，而不是让 `v-html` 收到
 * `"null"` 那个字面量。
 */
export function renderMarkdown(text: string | null | undefined): string {
  if (!text) return ''
  return markdown.render(text)
}
