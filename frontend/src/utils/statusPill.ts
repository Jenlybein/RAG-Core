import type { CSSProperties } from 'vue'

/**
 * 账号 / Key 这类「生效中 / 已停用」状态徽章的唯一样式来源。
 *
 * 抽出来的原因很具体：这套配色原本在 `AccountsPage.vue`（只给文字色）与
 * `KeysPage.vue`（自己拼一个胶囊）各写了一份，而且都还是重构前的冷色（#D1FAE5 /
 * #065F46）。同一件事两处实现，改一次就会漏一处——所以收成一份，两个页面都读它。
 *
 * 颜色沿用 `StatusBadge.vue` 那套暖化后的语义色，四组对比度均 ≥4.5:1。
 *
 * 返回类型是 Vue 的 `CSSProperties` 而不是自定义 interface：自定义 interface 直接被
 * 绑到 `:style` 上时，模板的类型检查会认为它不满足 `StyleValue`（属性名的大小写与
 * 联合类型都对不上），于是整行报错——用官方类型就从根上避开这件事。
 */

/** 两档就够：`active` 与「其余」。后端将来加状态时不会静默显示成绿色。 */
const ACTIVE: CSSProperties = { backgroundColor: '#E4EDDD', color: '#3C5228' }
const INACTIVE: CSSProperties = { backgroundColor: '#F0ECE4', color: '#6B6558' }

/** 对外文案：未知状态原样显示，不猜（猜错会让人以为账号「正常」）。 */
const LABELS: Record<string, string> = { active: '正常', disabled: '已停用' }

export function statusPillStyle(status: string | null | undefined): CSSProperties {
  return status === 'active' ? ACTIVE : INACTIVE
}

export function statusLabel(status: string | null | undefined): string {
  if (status === null || status === undefined) return ''
  return LABELS[status] ?? status
}

/** Key 列表只需两档说法：生效中 / 其余原样。 */
export function keyStatusLabel(status: string | null | undefined): string {
  if (status === 'active') return '生效中'
  return statusLabel(status)
}
