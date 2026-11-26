/**
 * ECharts 的**按需注册**（第 14 步 #203）。
 *
 * 为什么不是 `import * as echarts from 'echarts'`：全量引入 gzip 后多 300KB+，
 * 而这一层只用到柱状、折线、网格、提示、图例与 Canvas 渲染器。必须在**一个**文件里
 * `use()`：散在各页面里注册会让「哪些图表被引入」变成没人说得清的事。
 *
 * 配色从 CSS 变量里读（`tokens.css`），不在这里写死：主题（跟随系统 / 浅 / 深）由
 * `lib/theme.ts` 写进 `<html data-theme>`，图表的坐标轴、网格线、文字都要跟着它走，
 * 否则深色页面上会出现一块浅色的图。
 */

import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

export { echarts }

export interface ChartPalette {
  ink: string
  muted: string
  line: string
  accent: string
  warm: string
  violet: string
  danger: string
  surface: string
  font: string
}

/**
 * 三本账的**身份色**：图上系列色与额度卡上的方点必须同源。
 *
 * 检索走主题色（跟着 accent 变），重排与模型各有一个固定色——它们没有对应的语义令牌，
 * 也不该有：这两个色只回答「这是哪一本账」，不表达状态。写在这里而不是各页面各写一遍，
 * 否则同一个方点在两个地方会是两个颜色（`--c-star` 在深色下是 #ffd60a，与这里的 #ff9f0a 不同）。
 */
export const SERIES_ACCENT = 'rgb(var(--c-accent))'
export const SERIES_WARM = '#ff9f0a'
export const SERIES_VIOLET = '#5e5ce6'
/** 第四本账（切块）：与前三本同属「用量」系列，颜色区分只为了在四栏里分得开。 */
export const SERIES_TEAL = '#30b0c7'

const FALLBACK: ChartPalette = {
  ink: '#1d1d1f',
  muted: '#86868b',
  line: '#e5e5ea',
  accent: '#0071e3',
  warm: '#ff9f0a',
  violet: '#5e5ce6',
  danger: '#ff3b30',
  surface: '#ffffff',
  font: '-apple-system, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif',
}

/** 令牌是 RGB 三元组，读出来裹成 `rgb()`；空值或已是完整色值时原样返回。 */
function readVar(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  const value = styles.getPropertyValue(name).trim()
  if (value === '') return fallback
  // 「210 210 215」→「rgb(210 210 215)」；「#e5e5ea」这类完整值不重复包裹。
  return /^[\d.\s]+$/.test(value) ? `rgb(${value})` : value
}

/**
 * 读当前主题下的实际颜色。每次重画都读一遍：主题可能刚被切换。
 *
 * 令牌是 **RGB 三元组**（`--c-ink: 232 236 243`），所以这里要裹一层 `rgb()`；
 * 读 `--c-*` 而不是 `--ink` / `--accent` 这些旧别名——那些别名只存在于设计稿的
 * `tokens.css` 里，应用侧从来没有，照着读只会一路落到 FALLBACK，深色下图表就用
 * 浅色的字与线画在深底上（看起来像没上色）。
 */
export function chartPalette(): ChartPalette {
  if (typeof window === 'undefined') return FALLBACK
  const styles = window.getComputedStyle(document.documentElement)
  return {
    ink: readVar(styles, '--c-ink', FALLBACK.ink),
    muted: readVar(styles, '--c-muted', FALLBACK.muted),
    line: readVar(styles, '--c-line', FALLBACK.line),
    accent: readVar(styles, '--c-accent', FALLBACK.accent),
    warm: SERIES_WARM,
    violet: SERIES_VIOLET,
    danger: readVar(styles, '--c-danger', FALLBACK.danger),
    surface: readVar(styles, '--c-surface', FALLBACK.surface),
    font: readVar(styles, '--font-sans', FALLBACK.font),
  }
}

/**
 * 主题一变就重画。`lib/theme.ts` 改的是 `<html data-theme>`，这里盯它的属性；
 * 系统偏好变化由那边写属性时同样会触发，所以不必额外监听 `matchMedia`。
 */
export function onThemeChange(callback: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  const observer = new MutationObserver(() => callback())
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => observer.disconnect()
}
