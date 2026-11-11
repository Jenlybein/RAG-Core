<script setup lang="ts">
/**
 * 观测总览（管理员）：一屏回答「今天查了多少、慢在哪、谁在拖、质量如何」。
 *
 * 两层数据分开呈现，**不合并成一个数**：
 *
 * - **本地事实层**：来自我们自己的库，零外部依赖。观测栈挂了照样有数——这正是这张页面
 *   存在的意义：栈挂的那一刻，管理员最需要看到「最后一次成功的请求是几点」。
 * - **观测栈层**：六段耗时的分位数，只有 `TRACE_SINK=langfuse` 且凭据齐全时才取得到。
 *   取不到时显示原因，**绝不画成 0** —— 一屏 0 毫秒会被读成「检索变快了」。
 *
 * 图表不引图表库：数据量就是几十个点，一个手写的 SVG 折线/柱状足够，而 chart.js 这类
 * 库光 gzip 就 60KB，为两张小图不值得。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'

import PageHeader from '../../components/PageHeader.vue'
import {
  OBS_WINDOWS,
  fetchObservabilityOverview,
  fetchObservabilityStack,
  fetchObservabilityStages,
  observabilityOverviewQueryKey,
  observabilityStackQueryKey,
  observabilityStagesQueryKey,
  type ObsWindow,
} from '../../services/adminObservability'
import { errorText } from '../../utils/errorText'
import { formatPercent } from '../../utils/format'

const hours = ref<ObsWindow>(24)

const overviewQuery = useQuery({
  queryKey: computed(() => observabilityOverviewQueryKey(hours.value)),
  queryFn: () => fetchObservabilityOverview(hours.value),
})
const stagesQuery = useQuery({
  queryKey: computed(() => observabilityStagesQueryKey(hours.value)),
  queryFn: () => fetchObservabilityStages(hours.value),
})
const stackQuery = useQuery({
  queryKey: observabilityStackQueryKey(),
  queryFn: fetchObservabilityStack,
})

const overview = computed(() => overviewQuery.data.value ?? null)
const stages = computed(() => stagesQuery.data.value ?? null)
const stack = computed(() => stackQuery.data.value ?? null)
const loadError = computed(() =>
  overviewQuery.isError.value ? errorText(overviewQuery.error.value) : '',
)

// —— KPI ——
const degradedRate = computed(() => {
  const data = overview.value
  if (data === null || data.total === 0) return null
  return data.degraded / data.total
})

function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(0)} ms`
}

function rate(value: number | null | undefined): string {
  // 空值走 `formatPercent` 的「—」，不要 `?? 0`：0% 与「没有数据」在监控里是两件事。
  return formatPercent(value)
}

// —— 趋势图（手写 SVG polyline）——
const CHART_W = 100
const CHART_H = 28

/** 把小时桶折成折线坐标；空档（没有数据的小时）**不补 0**，折线在那里断开。 */
const trendPoints = computed(() => {
  const rows = overview.value?.hourly ?? []
  if (rows.length === 0) return ''
  const max = Math.max(...rows.map((row) => row.total), 1)
  return rows
    .map((row, index) => {
      const x = rows.length === 1 ? 0 : (index / (rows.length - 1)) * CHART_W
      const y = CHART_H - (row.total / max) * CHART_H
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
})

const trendMax = computed(() => {
  const rows = overview.value?.hourly ?? []
  return rows.length === 0 ? 0 : Math.max(...rows.map((row) => row.total))
})

// —— 评分分布 ——
const ratingBars = computed(() => {
  const histogram = overview.value?.ratings.histogram ?? {}
  const values = [1, 2, 3, 4, 5].map((score) => Number(histogram[String(score)] ?? 0))
  const max = Math.max(...values, 1)
  return [1, 2, 3, 4, 5].map((score, index) => ({
    score,
    count: values[index],
    heightPct: (values[index] / max) * 100,
  }))
})

// —— 六段 ——
const stageRows = computed(() =>
  (stages.value?.stages ?? []).filter((stage) => stage.samples > 0),
)
const stageUnavailable = computed(
  () => stages.value !== null && stages.value.source === 'none',
)
const stageReasonText = computed(() => {
  const reason = stages.value?.reason
  if (reason === 'stack_not_configured') {
    return '观测栈未接入：把 TRACE_SINK 设为 langfuse 并配好凭据后，这里才有数据。'
  }
  if (reason === 'stack_unreachable') {
    return '观测栈这次取不到：可能不可达或凭据失效。本地事实层的数字不受影响。'
  }
  return '观测栈层暂无数据。'
})

function onWindowChange(value: ObsWindow): void {
  hours.value = value
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader title="观测" subtitle="跨租户的检索量、耗时与质量；数字不参与发布判定。">
      <template #actions>
        <el-select
          :model-value="hours"
          size="small"
          class="w-28"
          @update:model-value="onWindowChange"
        >
          <el-option v-for="option in OBS_WINDOWS" :key="option" :value="option" :label="`近 ${option} 小时`" />
        </el-select>
        <el-button size="small" :loading="overviewQuery.isFetching.value" @click="overviewQuery.refetch()">
          刷新
        </el-button>
      </template>
    </PageHeader>

    <p v-if="loadError !== ''" class="card card-body text-sm text-red-700">{{ loadError }}</p>

    <template v-else>
      <!-- KPI -->
      <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div class="card card-body">
          <p class="metric-label">检索次数</p>
          <p class="metric-value mt-1 text-[26px]">{{ overview?.total ?? '—' }}</p>
          <p class="mt-1 text-xs text-muted">窗口内写入归属索引的条数</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">P50 耗时</p>
          <p class="metric-value mt-1 text-[26px]">{{ ms(overview?.latency.p50) }}</p>
          <p class="mt-1 text-xs text-muted">P95 {{ ms(overview?.latency.p95) }} · 最慢 {{ ms(overview?.latency.worst) }}</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">降级率</p>
          <p class="metric-value mt-1 text-[26px]">{{ rate(degradedRate) }}</p>
          <p class="mt-1 text-xs text-muted">{{ overview?.degraded ?? 0 }} 次走到降级（关键词那一路挂了等）</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">平均评分</p>
          <p class="metric-value mt-1 text-[26px]">
            {{ overview?.ratings.average === null || overview?.ratings.average === undefined ? '—' : overview.ratings.average }}
          </p>
          <p class="mt-1 text-xs text-muted">{{ overview?.ratings.count ?? 0 }} 条评分</p>
        </div>
      </div>

      <div class="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div class="space-y-6">
          <!-- 趋势 -->
          <section class="card card-body">
            <div class="flex items-baseline justify-between gap-2">
              <h2 class="section-title">每小时检索量</h2>
              <span class="text-xs text-muted">峰值 {{ trendMax }}</span>
            </div>
            <div v-if="trendPoints === ''" class="py-10 text-center text-sm text-muted">
              窗口内还没有检索记录。
            </div>
            <svg
              v-else
              class="mt-4 h-32 w-full"
              :viewBox="`0 0 ${CHART_W} ${CHART_H}`"
              preserveAspectRatio="none"
              role="img"
              aria-label="每小时检索量折线"
            >
              <polyline
                :points="trendPoints"
                fill="none"
                stroke="#0071e3"
                stroke-width="0.8"
                vector-effect="non-scaling-stroke"
                stroke-linejoin="round"
              />
            </svg>
          </section>

          <!-- 六段 -->
          <section class="card card-body">
            <div class="flex items-baseline justify-between gap-2">
              <h2 class="section-title">六段耗时</h2>
              <span class="chip">{{ stages?.source === 'langfuse' ? '来自观测栈' : '未接入' }}</span>
            </div>

            <p v-if="stageUnavailable" class="mt-3 rounded-control bg-surface-muted px-3 py-2 text-sm text-muted">
              {{ stageReasonText }}
            </p>
            <div v-else class="mt-4 overflow-x-auto">
              <table class="w-full border-collapse text-sm">
                <thead>
                  <tr>
                    <th class="th-label border-b border-line px-2 py-2 text-left">段</th>
                    <th class="th-label border-b border-line px-2 py-2 text-right">样本</th>
                    <th class="th-label border-b border-line px-2 py-2 text-right">P50</th>
                    <th class="th-label border-b border-line px-2 py-2 text-right">P95</th>
                    <th class="th-label border-b border-line px-2 py-2 text-right">占六段之和</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="stage in stageRows" :key="stage.name" class="border-t border-line">
                    <td class="px-2 py-2 font-mono text-xs text-ink">{{ stage.name }}</td>
                    <td class="px-2 py-2 text-right text-xs text-muted">{{ stage.samples }}</td>
                    <td class="px-2 py-2 text-right tabular-nums">{{ ms(stage.p50) }}</td>
                    <td class="px-2 py-2 text-right tabular-nums text-muted">{{ ms(stage.p95) }}</td>
                    <td class="px-2 py-2 text-right tabular-nums text-muted">{{ rate(stage.share) }}</td>
                  </tr>
                </tbody>
              </table>
              <p class="mt-3 text-xs text-muted">
                观测栈里是我们自己写进去的 <code class="mono-tag">duration_ms</code>；
                与本地层的「总耗时」不是同一个口径，两边数字不会严格相等。
              </p>
            </div>
          </section>

          <!-- 租户榜 -->
          <section class="card card-body">
            <h2 class="section-title">租户榜（按调用量）</h2>
            <div v-if="(overview?.by_tenant.length ?? 0) === 0" class="py-8 text-center text-sm text-muted">
              窗口内没有记录。
            </div>
            <table v-else class="mt-3 w-full border-collapse text-sm">
              <thead>
                <tr>
                  <th class="th-label border-b border-line px-2 py-2 text-left">租户</th>
                  <th class="th-label border-b border-line px-2 py-2 text-right">检索</th>
                  <th class="th-label border-b border-line px-2 py-2 text-right">降级率</th>
                  <th class="th-label border-b border-line px-2 py-2 text-right">平均耗时</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in overview?.by_tenant ?? []" :key="row.tenant_id" class="border-t border-line">
                  <td class="px-2 py-2 font-mono text-xs text-ink">{{ row.tenant_id }}</td>
                  <td class="px-2 py-2 text-right tabular-nums">{{ row.total }}</td>
                  <td class="px-2 py-2 text-right tabular-nums" :class="row.degraded_rate !== null && row.degraded_rate > 0.1 ? 'text-[#7a5b00]' : 'text-muted'">
                    {{ rate(row.degraded_rate) }}
                  </td>
                  <td class="px-2 py-2 text-right tabular-nums text-muted">{{ ms(row.avg_latency_ms) }}</td>
                </tr>
              </tbody>
            </table>
          </section>
        </div>

        <!-- 右栏：评分分布 + 观测栈状态 -->
        <div class="space-y-6">
          <section class="card card-body">
            <h2 class="section-title">评分分布</h2>
            <div class="mt-4 flex h-28 items-end justify-between gap-2">
              <div v-for="bar in ratingBars" :key="bar.score" class="flex flex-1 flex-col items-center gap-1">
                <span class="text-xs text-muted">{{ bar.count }}</span>
                <div class="w-full rounded-t bg-accent/15" :style="{ height: `${Math.max(bar.heightPct, 2)}%` }">
                  <div class="h-full w-full rounded-t bg-accent" :style="{ opacity: bar.count === 0 ? 0 : 1 }" />
                </div>
                <span class="text-xs text-muted">{{ bar.score }}★</span>
              </div>
            </div>
            <p class="mt-3 text-xs text-muted">未打分的检索不出现在这里。</p>
          </section>

          <section class="card card-body">
            <h2 class="section-title">观测栈</h2>
            <dl class="mt-3 space-y-2 text-sm">
              <div class="flex items-baseline justify-between gap-2">
                <dt class="text-muted">去向</dt>
                <dd class="font-mono text-xs text-ink">{{ stack?.sink ?? '—' }}</dd>
              </div>
              <div class="flex items-baseline justify-between gap-2">
                <dt class="text-muted">凭据</dt>
                <dd class="text-xs" :class="stack?.has_keys ? 'text-ink' : 'text-[#7a5b00]'">
                  {{ stack?.has_keys ? '已配置' : '未配置' }}
                </dd>
              </div>
              <div class="flex items-baseline justify-between gap-2">
                <dt class="text-muted">观测栈层</dt>
                <dd class="text-xs" :class="stack?.stack_layer_available ? 'text-ink' : 'text-muted'">
                  {{ stack?.stack_layer_available ? '可用' : '不可用' }}
                </dd>
              </div>
            </dl>
            <a
              v-if="stack?.langfuse_host"
              :href="stack.langfuse_host"
              target="_blank"
              rel="noopener noreferrer"
              class="mt-4 inline-block text-sm text-brand underline"
            >
              打开 Langfuse →
            </a>
            <p class="mt-3 text-xs text-muted">
              这一页不显示密钥；看单个 trace 的细节请去 Langfuse 或「检索记录」。
            </p>
          </section>
        </div>
      </div>
    </template>
  </section>
</template>
