/**
 * 控制台瀑布图的响应形状（`app/schemas/console_traces.py` 的 `RetrievalWaterfallData`）。
 *
 * 只声明本页会读的字段：多写的字段进不了前端，少写的会在使用处被 TS 拦住——这比一个
 * `unknown` 更早暴露契约漂移。
 *
 * 三条不能自己发明的语义：
 *
 * 1. `reason` 为 `null` **当且仅当** `stages` 非空（后端保证）。所以「有段」与「没有段」
 *    只看 `stages.length`，不要用 `enabled` 判断；
 * 2. `enabled` 说的是**当前** `TRACE_SINK` 配置，不是这条记录写下时的配置——关闭观测之后
 *    本地旧记录照样看得到（那时 `enabled=false` 而 `stages` 非空）；
 * 3. `latency_ms` 是归属索引里记的**总耗时**，与六段之和不相等：总耗时还包含链路之外的开销，
 *    没发生的段也不补 0。
 */

/** 一段：名字与毫秒数。名字取自后端六段词表（英文原样显示）。 */
export interface StageTiming {
  name: string
  duration_ms: number
}

/**
 * 没有段时的原因：
 * - `sink_disabled`：观测没开（`TRACE_SINK=none`），界面上说「未启用」；
 * - `no_record`：观测开着，但这条记录两处都没有，界面上说「无记录」。
 * 后端将来加值时不希望前端白屏，所以这里留了 `string` 兜底（未知值按「无记录」显示）。
 */
export type WaterfallReason = 'sink_disabled' | 'no_record' | string

/** 段从哪读来的；`null` 表示一段都没读到。 */
export type WaterfallSource = 'jsonl' | 'langfuse' | string | null

/** `GET /console/retrieval-traces/{log_id}/waterfall` 的 `data`。 */
export interface RetrievalWaterfall {
  log_id: string
  trace_id: string
  enabled: boolean
  source: WaterfallSource
  reason: WaterfallReason | null
  /** 一句给人看的说明（观测服务读不到时才有）；机器判据用 `reason`，不要解析这句话。 */
  note: string | null
  latency_ms: number
  created_at: string | null
  stages: StageTiming[]
}
