/**
 * 用量看板的显示格式（第 14 步 #203）。
 *
 * 为什么又开一个 format 文件而不塞进 `utils/format.ts`：那个文件服务的是**检索结果**
 * （分数、名次、相似度），口径是「未命中折成 —」；这里服务的是**用量**，口径是
 * 「没有样本折成 —，但 0 就是 0」。两种「空」的语义不同，混在一个模块里迟早有人用错。
 */

/** 毫秒：≥ 1000 走秒（一位小数），否则整数毫秒。`null` → `—`。 */
export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`
}

/** 计数：千分位。0 仍然是 `0`——「没有调用」是一个确定的数，不是「没有数据」。 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString('zh-CN')
}

/**
 * 百分比。
 *
 * 与 `formatPercent` 的区别：那个收 0~1 的比值，这个收**已经乘过 100** 的值
 * （后端 `success_rate` 是比值，直方图分位是数；两者混用最容易差 100 倍）。
 * 这里统一收比值，一位小数。
 */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(1)}%`
}

/** 数据来源的说明，直接显示在标题旁边，避免「这数哪来的」被反复问。 */
export function sourceLabel(source: 'events' | 'daily'): string {
  return source === 'daily' ? '按天汇总' : '逐条明细'
}

/** P95 的成色：近似值必须标出来（误差约 13%）；算不出来时不能冒充「没有样本」。 */
export function p95Label(source: 'exact' | 'histogram' | 'histogram_missing'): string {
  if (source === 'histogram') return 'P95（直方图近似）'
  if (source === 'histogram_missing') return 'P95（汇总缺失）'
  return 'P95'
}
