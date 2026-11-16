import type { StageTiming } from '../types/traceWaterfall'

/**
 * 瀑布图的纯函数：段名 → 中文说明、毫秒数 → 条宽。
 *
 * 抽成纯函数（而不是写在组件模板里）的理由与 `utils/format.ts` 一致：模板里的一句
 * `duration / total * 100` 一旦要处理「没有段」「全部为 0」两种边界，就已经不是一行了，
 * 而这些边界恰好是最容易画错的（除零得到 `NaN`，宽度 `NaN%` 会让整条消失）。
 *
 * **宽度是「占六段合计的比例」，不是「占最大段的比例」**：票面写的是「宽度按时长占比」，
 * 一个 88ms 的重排在 149ms 的六段里就该占 59%，而不是占满整行——后者会让人以为这一段
 * 就是全部耗时。侧边那列毫秒数是精确值，条只是它的形状。
 */

/** 六段的中文说法（`GLOSSARY.md`「检索链路」）。英文段名照原文显示，中文只进 tooltip。 */
export const STAGE_LABELS: Record<string, string> = {
  query_optimization: '查询优化',
  vector_recall: '向量召回',
  keyword_recall: '关键词召回',
  fusion: '融合',
  rerank: '重排',
  collapse: '折叠',
}

/** 一行瀑布：段名、中文说明、条宽（百分比字符串）、毫秒数（保留 1 位）。 */
export interface WaterfallRow {
  name: string
  label: string
  width: string
  ms: string
}

/** 六段合计（后端不补 0，所以「合计」只是这几段的和，不是总耗时）。 */
export function totalOf(stages: StageTiming[]): number {
  return stages.reduce((sum, stage) => sum + stage.duration_ms, 0)
}

/**
 * 哪几段是**并行**的：组内同时开始，所以组内耗时只该算最长的那一支。
 *
 * 现在只有一组：混合检索的向量一路与关键词一路同时发出（`asyncio.gather`）。少了这条
 * 知识，把六段一加就会算出「合计 13261ms」而这次请求只花了 7710ms——读的人会以为数据
 * 错了，或者以为自己看漏了一段。
 */
export const PARALLEL_STAGE_GROUPS: readonly (readonly string[])[] = [
  ['vector_recall', 'keyword_recall'],
]

/**
 * 关键路径耗时：顺序段相加，并行组取最长的一支。
 *
 * 它回答的是「这次请求最少花了多久」，与 `totalOf`（各段简单相加）不是一回事：后者受
 * 并行影响会偏大，只用来说明「一条条算下来花了多少活」。两者都有用，别混用。
 *
 * 组里只到了一半（另一路没发生或没记上）时**不抵扣**：那时它就不是并行了，按顺序算
 * 才是诚实的。
 */
export function elapsedOf(stages: StageTiming[]): number {
  const byName = new Map(stages.map((stage) => [stage.name, stage.duration_ms]))
  let total = totalOf(stages)
  for (const group of PARALLEL_STAGE_GROUPS) {
    const durations = group.map((name) => byName.get(name))
    if (durations.every((value) => value !== undefined)) {
      total -= Math.min(...(durations as number[]))
    }
  }
  return total
}

/**
 * 毫秒 → 条宽。合计为 0 时一律给 0%：一段都没耗时的时候，画任何宽度都是在编数据。
 *
 * 这里**不设最小宽度**：比例就是比例；「太短看不见」由 CSS 的 `min-width` 兜（那是显示
 * 问题，不是数据问题）。
 */
export function barWidth(durationMs: number, totalMs: number): string {
  if (!(totalMs > 0)) return '0%'
  const ratio = Math.max(0, durationMs) / totalMs
  // 四位小数足够画出 0.01% 的一小段，又不会让 style 里出现一长串数字。
  return `${(ratio * 100).toFixed(4)}%`
}

/** 把后端给的段摊成要渲染的行；顺序就是后端给的链路顺序，不在这里重排。 */
export function toWaterfallRows(stages: StageTiming[]): WaterfallRow[] {
  const total = totalOf(stages)
  return stages.map((stage) => ({
    name: stage.name,
    label: STAGE_LABELS[stage.name] ?? '未收录的段名',
    width: barWidth(stage.duration_ms, total),
    ms: stage.duration_ms.toFixed(1),
  }))
}

/** 来源的中文说法：`jsonl` 是本地记录，`langfuse` 是观测服务。 */
export function describeSource(source: string | null): string {
  if (source === 'jsonl') return '本地记录'
  if (source === 'langfuse') return '观测服务'
  return '—'
}
