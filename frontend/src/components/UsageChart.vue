<script setup lang="ts">
/**
 * 用量图（第 14 步 #203）：两种形态，一个组件。
 *
 * - `stacked`：**四本账**（检索 / 重排 / 大模型 三条堆叠柱 = 次数，加切块一条线 = token）；
 * - `quality`：成功率（左轴 %）+ P95（右轴 ms）——「好不好」。
 *
 * 三件刻意为之的事：
 *
 * 1. **空窗口不画 0**：一个桶里 `requests = 0` 时成功率不给点（`null`），折线在那里断开。
 *    补成 0% 会被读成「全挂了」。
 * 2. **配色跟着主题走**：颜色从 `tokens.css` 的变量里读（`lib/charts.ts`），主题一变重画，
 *    否则深色页面上会出现一块浅色的图。
 * 3. **容器固定高度**：图表初始化前后高度一致，卡片不会「加载时忽然长高」。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { chartPalette, echarts, onThemeChange } from '../lib/charts'
import type { UsageBucket } from '../types/usage'

const props = withDefaults(
  defineProps<{
    buckets: UsageBucket[]
    kind?: 'stacked' | 'quality'
    /** 图形高度（px）。三档够用了：小卡片 160、常规 220、主角 280。 */
    height?: number
  }>(),
  { kind: 'stacked', height: 220 },
)

const host = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let disposeThemeWatch: (() => void) | null = null

function percentOf(bucket: UsageBucket): number | null {
  // 分母只算结局已知的请求：没走完的桶里，`failed` 也还没算上。
  const known = bucket.ok + bucket.failed
  return known === 0 ? null : (bucket.ok / known) * 100
}

function option() {
  const palette = chartPalette()
  const labels = props.buckets.map((item) => item.bucket)
  const axisCommon = {
    axisLine: { lineStyle: { color: palette.line } },
    axisTick: { show: false },
    axisLabel: { color: palette.muted, fontSize: 11, fontFamily: palette.font },
    splitLine: { lineStyle: { color: palette.line, type: 'dashed' as const } },
  }
  const base = {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: palette.font },
    grid: { left: 4, right: 8, top: 28, bottom: 4, containLabel: true },
    tooltip: {
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
      backgroundColor: palette.surface,
      borderColor: palette.line,
      textStyle: { color: palette.ink, fontSize: 12, fontFamily: palette.font },
    },
    legend: {
      show: true,
      top: 0,
      left: 0,
      itemWidth: 8,
      itemHeight: 8,
      itemGap: 14,
      textStyle: { color: palette.muted, fontSize: 11, fontFamily: palette.font },
    },
    xAxis: { type: 'category' as const, data: labels, ...axisCommon, splitLine: { show: false } },
  }

  if (props.kind === 'quality') {
    return {
      ...base,
      // 图例放上面、右轴刻度留给自己：两者挤在一起会叠字。
      legend: { ...base.legend, show: true },
      yAxis: [
        {
          type: 'value' as const,
          min: 0,
          max: 100,
          // 顺序要紧：展开 `axisCommon` 之后再覆盖 axisLabel，否则右边那个会被覆盖掉。
          ...axisCommon,
          axisLabel: { ...axisCommon.axisLabel, formatter: '{value}%' },
        },
        {
          type: 'value' as const,
          splitLine: { show: false },
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            ...axisCommon.axisLabel,
            formatter: (value: number) => `${value}ms`,
          },
        },
      ],
      series: [
        {
          name: '成功率',
          type: 'line' as const,
          smooth: true,
          symbol: 'none',
          yAxisIndex: 0,
          connectNulls: false,
          lineStyle: { width: 2, color: palette.accent },
          itemStyle: { color: palette.accent },
          data: props.buckets.map(percentOf),
        },
        {
          name: 'P95',
          type: 'line' as const,
          smooth: true,
          symbol: 'none',
          yAxisIndex: 1,
          connectNulls: false,
          lineStyle: { width: 2, type: 'dotted' as const, color: palette.violet },
          itemStyle: { color: palette.violet },
          data: props.buckets.map((item) => item.p95Ms),
        },
      ],
    }
  }

  return {
    ...base,
    // **两个 Y 轴**：左轴是「调用了几次」（前三本），右轴是「烧了多少 token」（第四本）。
    // 这不是排版选择而是量纲约束——把 token 堆进那三根柱子里，柱高就是「次数 + token」，
    // 一个没有意义的和。所以第四本单独一条线，图例里标明单位。
    yAxis: [
      { type: 'value' as const, minInterval: 1, ...axisCommon },
      {
        type: 'value' as const,
        minInterval: 1,
        ...axisCommon,
        // 右轴不画横向网格线：两条轴的刻度线叠在一起会让读数分辨率糊掉。
        splitLine: { show: false },
        // 右轴是 token：整库重建一次就是几十万，不折行显示会撑爆轴线宽度。
        axisLabel: {
          ...axisCommon.axisLabel,
          formatter: (value: number) =>
            value >= 10000 ? `${(value / 10000).toFixed(0)}万` : String(value),
        },
        name: 'token',
        nameTextStyle: { color: palette.muted, fontSize: 11, fontFamily: palette.font },
      },
    ],
    series: [
      {
        name: '检索',
        type: 'bar' as const,
        stack: 'meters',
        barMaxWidth: 22,
        itemStyle: { color: palette.accent, borderRadius: [0, 0, 0, 0] },
        data: props.buckets.map((item) => item.meterR),
      },
      {
        name: '重排',
        type: 'bar' as const,
        stack: 'meters',
        barMaxWidth: 22,
        itemStyle: { color: palette.warm },
        data: props.buckets.map((item) => item.meterRr),
      },
      {
        name: '大模型',
        type: 'bar' as const,
        stack: 'meters',
        barMaxWidth: 22,
        itemStyle: { color: palette.violet, borderRadius: [3, 3, 0, 0] },
        data: props.buckets.map((item) => item.meterLlm),
      },
      {
        // 第四本账：切块（**token**，右轴）。用折线而不是第四根柱——它不是同一根柱子上的
        // 一段，而是另一把尺子；画成柱会被人下意识地和旁边那根堆叠柱比高低。
        name: '切块（token）',
        type: 'line' as const,
        smooth: false,
        symbol: 'circle',
        symbolSize: 4,
        yAxisIndex: 1,
        connectNulls: false,
        lineStyle: { width: 1.5, color: palette.teal },
        itemStyle: { color: palette.teal },
        data: props.buckets.map((item) => item.meterChunk),
      },
    ],
  }
}

function render(): void {
  if (chart === null) return
  chart.setOption(option(), { notMerge: true })
}

function resize(): void {
  chart?.resize()
}

onMounted(() => {
  if (host.value === null) return
  chart = echarts.init(host.value)
  render()
  disposeThemeWatch = onThemeChange(render)
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  disposeThemeWatch?.()
  chart?.dispose()
  chart = null
})

watch(() => props.buckets, render, { deep: true })
watch(() => props.kind, render)
</script>

<template>
  <div
    ref="host"
    class="w-full"
    :style="{ height: `${height}px` }"
    role="img"
    :aria-label="kind === 'quality' ? '成功率与 P95 耗时趋势' : '四本账用量趋势（右轴为切块 token）'"
  />
</template>
