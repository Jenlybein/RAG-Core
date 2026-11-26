/**
 * 主题三态：跟随系统 / 浅 / 深。
 *
 * 为什么不用纯 CSS（`:has()` + radio）那套：这里已经有 JS，用 JS 把 `auto` **解析**成
 * 实际值再写 `data-theme`，`tokens.css` 就只需要一份深色变量——纯 CSS 方案要为
 * 「媒体查询」和「手动选择」各留一份副本，三处改色必有一处漏。
 *
 * 同时维护 Element Plus 认的 `dark` class：它的官方深色主题
 * （`element-plus/theme-chalk/dark/css-vars.css`）就是按 `.dark` 选择器写的。
 */

export type ThemeMode = 'auto' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'rag-console.theme'

const MODES: readonly ThemeMode[] = ['auto', 'light', 'dark']

/** 存储坏了 / 首次访问 / 用户手改了值：一律回落到「跟随系统」，不要抛。 */
export function readMode(): ThemeMode {
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY)
    return raw !== null && (MODES as readonly string[]).includes(raw) ? (raw as ThemeMode) : 'auto'
  } catch {
    return 'auto'
  }
}

/**
 * `auto` → 系统的实际偏好。纯函数，便于单测（不碰 window）。
 */
export function resolveTheme(mode: ThemeMode, systemPrefersDark: boolean): ResolvedTheme {
  if (mode === 'dark') return 'dark'
  if (mode === 'light') return 'light'
  return systemPrefersDark ? 'dark' : 'light'
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}

/**
 * 写进 DOM 并返回实际生效的值。
 * `data-theme` 给 tokens.css，`dark` class 给 Element Plus——两套生态各认各的。
 */
export function applyTheme(mode: ThemeMode): ResolvedTheme {
  const actual = resolveTheme(mode, systemPrefersDark())
  const root = document.documentElement
  root.dataset.theme = actual
  root.classList.toggle('dark', actual === 'dark')

  // 浏览器自绘的两处：状态栏颜色与原生控件（滚动条、表单）的明暗
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  if (meta !== null) meta.content = actual === 'dark' ? '#14161a' : '#e4e8ef'
  return actual
}

export function setMode(mode: ThemeMode): ResolvedTheme {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode)
  } catch {
    // 隐私模式 / 存储被禁：主题仍然生效，只是下次打开回到 auto
  }
  return applyTheme(mode)
}

/**
 * 启动时调用一次。返回清理函数：系统主题变化时，`auto` 要跟着变；
 * `light` / `dark` 是用户明确选的，系统怎么变都不动。
 */
export function initTheme(): () => void {
  let mode = readMode()
  applyTheme(mode)

  const query = window.matchMedia?.('(prefers-color-scheme: dark)')
  if (query === undefined) return () => {}

  const onChange = (): void => {
    if (mode === 'auto') applyTheme(mode)
  }
  query.addEventListener('change', onChange)

  // 别的标签页改了选择：同步过来，避免两个窗口一深一浅
  const onStorage = (event: StorageEvent): void => {
    if (event.key !== THEME_STORAGE_KEY) return
    mode = readMode()
    applyTheme(mode)
  }
  window.addEventListener('storage', onStorage)

  return () => {
    query.removeEventListener('change', onChange)
    window.removeEventListener('storage', onStorage)
  }
}
