<script setup lang="ts">
/**
 * 六段瀑布图（只读）：把 `GET /console/retrieval-traces/{log_id}/waterfall` 的结论摊开。
 *
 * 两条规矩：
 *
 * 1. **没有段时不许留空白**（票面验收第 5 条）。`reason` 是 `sink_disabled` 就说「未启用」，
 *    是 `no_record` 就说「有观测、但这条没记上」——「没配」与「没记上」在排查时是两条完全
 *    不同的路，画一张空卡片等于把这两条路合并成「不知道」；
 * 2. **不给数据补形状**。没发生的段不补 0（单模式下就没有 `fusion`），`latency_ms` 与六段
 *    之和也不强行对齐：两组数各自是事实，把它们凑成一个数字才是编数据。
 *
 * 颜色沿用页面既有令牌（`ink` / `muted` / `line` / `accent-soft`），不引新 UI 库。
 */
import { computed } from 'vue'

import type { RetrievalWaterfall } from '../types/traceWaterfall'
import {
  describeSource,
  toWaterfallRows,
  totalOf,
} from '../utils/waterfall'

const props = defineProps<{
  /** 后端原样给的瀑布；父组件保证不为 null（没发起查询时不渲染本组件）。 */
  data: RetrievalWaterfall
}>()

const rows = computed(() => toWaterfallRows(props.data.stages))
const totalMs = computed(() => totalOf(props.data.stages).toFixed(1))
const sourceLabel = computed(() => describeSource(props.data.source))
const hasStages = computed(() => props.data.stages.length > 0)

/**
 * 「为什么没有段」的标题与下一句。两句话必须不同，否则这条验收就白做了：
 * 一个说的是**这台机器没配**，另一个说的是**配了但这次没落下来**。
 */
const emptyCopy = computed(() => {
  if (props.data.reason === 'sink_disabled') {
    return {
      title: '观测未启用：这次没有阶段耗时可看',
      hint:
        '当前观测去向是 none（TRACE_SINK=none），本地也没有这条记录。' +
        '把 TRACE_SINK 设成 jsonl 或 langfuse、重跑一次检索，就能看到六段各花了多久。',
    }
  }
  if (props.data.reason === 'no_record') {
    return {
      title: '观测开着，但这条记录没留下阶段耗时',
      hint:
        '本地记录与观测服务里都没有这根 trace。常见原因：记录已过保留期被清掉，' +
        '或这次检索的观测写入失败（旁路失败只留 warning，检索本身仍会正常返回）。',
    }
  }
  // 后端的理由是「两处都有 / 都没有」的枚举；出现第三种就按最保守的说法显示，不猜。
  return {
    title: '这条记录里没有阶段耗时',
    hint: '后端没有给出原因；去看看服务端日志里与这根 trace_id 相关的 warning。',
  }
})
</script>

<template>
  <div>
    <div class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[11px]">
      <span class="flex flex-wrap items-baseline gap-2 text-muted">
        <span>
          来源
          <span class="font-mono text-slate-700">{{ sourceLabel }}</span>
        </span>
        <span class="text-line-strong">·</span>
        <span :title="data.trace_id">
          trace <span class="font-mono text-slate-700">{{ data.trace_id.slice(0, 8) }}</span>
        </span>
        <span class="text-line-strong">·</span>
        <span>retrieved {{ data.created_at === null ? '—' : data.created_at }}</span>
      </span>
      <span v-if="hasStages" class="text-muted">
        六段合计
        <span class="font-mono text-ink">{{ totalMs }}</span> ms ·
        本次总耗时
        <span class="font-mono text-ink">{{ data.latency_ms }}</span> ms
      </span>
    </div>

    <ul v-if="hasStages" class="mt-3 space-y-1.5">
      <li v-for="row in rows" :key="row.name" class="flex items-center gap-2.5">
        <!--
          英文段名 + 中文 tooltip，与「运行摘要」同一套做法：拿中文替身会让人拿着翻译去搜
          日志却搜不到；而第一次看的人需要一个中文说法。
        -->
        <span
          class="w-[9.5rem] shrink-0 truncate font-mono text-[11px] text-slate-700"
          :title="row.label"
        >
          {{ row.name }}
        </span>
        <span class="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-muted">
          <!-- 条自己最短 2px：0.01% 的段也该看得见，但宽度仍按真实占比算。 -->
          <span
            class="block h-2.5 min-w-[2px] rounded-full bg-accent"
            :style="{ width: row.width }"
          ></span>
        </span>
        <span class="w-14 shrink-0 text-right font-mono text-[11px] text-ink">
          {{ row.ms }} ms
        </span>
      </li>
    </ul>

    <!-- 没有段：明确说出「未启用」或「无记录」，不留空白卡片。 -->
    <div
      v-else
      class="mt-3 rounded-control border border-line bg-surface-muted px-3 py-2.5"
    >
      <p class="text-[12px] font-medium text-ink">{{ emptyCopy.title }}</p>
      <p class="mt-1 text-[11px] leading-relaxed text-slate-600">{{ emptyCopy.hint }}</p>
      <p class="mt-1.5 font-mono text-[10px] text-slate-400">
        enabled = {{ data.enabled }} · reason = {{ data.reason ?? '—' }}
      </p>
    </div>

    <!-- 观测服务读不到时的一句说明（后端只在那种情况下给 note）。 -->
    <p
      v-if="data.note !== null && data.note !== ''"
      class="mt-2 rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-[11px] leading-relaxed text-[#6E4E12]"
    >
      {{ data.note }}
    </p>

    <!--
      「有段、但观测现在是关的」：这份六段来自早先写下的本地记录。不说这一句的话，
      读的人会以为「关掉观测也照样在记」，下一次检索找不到记录时才来问为什么。
    -->
    <p v-if="hasStages && !data.enabled" class="mt-2 text-[11px] text-muted">
      当前观测去向是 none：以上六段来自本地记录，新的检索不会再记下新的阶段耗时。
    </p>
  </div>
</template>
