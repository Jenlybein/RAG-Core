import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './styles/main.css'

/**
 * 启动失败必须看得见。
 *
 * 之前任何一处模块级错误（依赖预构建缓存坏了、某个页面在 import 期就抛）的结果都是
 * **一片白**：`#app` 里什么都没有，控制台在用户眼里不存在，只能靠猜。调试台的定位是
 * 「出问题时告诉人发生了什么」，它自己坏掉的时候尤其不该沉默。
 *
 * 这个兜底刻意不依赖 Vue / Element Plus（它们可能正是坏掉的那一环）：纯 DOM + 内联样式，
 * 把错误信息原样摊在页面上。样式也刻意写死——外部 CSS 也可能没加载成功。
 */
function renderBootFailure(title: string, detail: string): void {
  const host = document.getElementById('app')
  if (host === null) return
  host.innerHTML = ''
  const panel = document.createElement('div')
  panel.setAttribute('role', 'alert')
  panel.style.cssText =
    'max-width:44rem;margin:12vh auto;padding:28px;border:1px solid #E7E2D7;border-radius:20px;' +
    'background:#fff;box-shadow:0 1px 2px rgba(28,26,21,.04),0 12px 32px -18px rgba(28,26,21,.18);' +
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;color:#1C1A15;"
  const heading = document.createElement('h1')
  heading.textContent = title
  heading.style.cssText = 'margin:0 0 8px;font-size:20px;font-weight:600;letter-spacing:-0.02em;'
  const hint = document.createElement('p')
  hint.textContent =
    '页面没能启动。把下面这段原文发给开发同学；硬刷新（Ctrl+Shift+R）通常能解决缓存导致的这类问题。'
  hint.style.cssText = 'margin:0 0 16px;font-size:13px;line-height:1.6;color:#6B6558;'
  const pre = document.createElement('pre')
  pre.textContent = detail
  pre.style.cssText =
    'margin:0;padding:14px;border-radius:12px;background:#FBFAF6;border:1px solid #E7E2D7;' +
    'font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word;color:#8C2F22;' +
    "font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;"
  panel.append(heading, hint, pre)
  host.append(panel)
}

function describe(reason: unknown): string {
  if (reason instanceof Error) return `${reason.name}: ${reason.message}\n${reason.stack ?? ''}`.trim()
  if (typeof reason === 'string') return reason
  try {
    return JSON.stringify(reason, null, 2)
  } catch {
    return String(reason)
  }
}

// 模块求值期就抛的错误（最常见：依赖预构建缓存与源码不一致）：DOMContentLoaded 之后
// `#app` 可能还是空的，所以两个时机都兜一次。
window.addEventListener('error', (event) => {
  if (event.error !== undefined && event.error !== null) {
    renderBootFailure('调试台启动失败', describe(event.error))
  }
})
window.addEventListener('unhandledrejection', (event) => {
  renderBootFailure('调试台启动失败', describe(event.reason))
})

const app = createApp(App)

// Vue 渲染期错误：已经挂载成功但某个组件抛了，同样摊到页面上，而不是留一块空白。
app.config.errorHandler = (error) => {
  renderBootFailure('页面渲染出错', describe(error))
}

// Pinia 现在只放骨架；T10 的「选中 Key 只进内存」会依赖它，所以底座先立起来。
app.use(createPinia())
app.use(router)
// 数据请求都走两个手写客户端；query 留给后续页面的缓存与轮询（T3/T4）。
app.use(VueQueryPlugin)
app.use(ElementPlus)

try {
  app.mount('#app')
} catch (error) {
  renderBootFailure('调试台启动失败', describe(error))
}
