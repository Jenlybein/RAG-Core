<script setup lang="ts">
/**
 * 检索记录页：本租户最近若干次检索的列表 + 单条的六段瀑布。
 *
 * **这一页补的是「你得先知道一个 id」这个缺口。** 在此之前，瀑布、评分、旁证三条路都要
 * 先拿到 `log_id` 或 `trace_id`——使用者只能回调用方系统里翻出来再抄过来。列表页把这件事
 * 变成「点一行」。
 *
 * 三条设计取舍：
 *
 * 1. **不显示原文。** 归属索引本来就不存问句与切片正文（ADR-0021 决策 1），所以这一页
 *    不需要脱敏，也就不存在「一小心把原文画上屏幕」这种可能。
 * 2. **「未评分」画成空档，不画 0 星。** 1~5 的取值域里没有 0（越界会被 `20022` 拒），
 *    把 `null` 渲染成 0 等于说「用户给了最差评」，那是另一种意思。
 * 3. **观测栈外链由服务端决定给不给。** 去向是 `jsonl` / `none` 时栈里根本没有这条 trace，
 *    前端自己拼链接只会点出 404。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'

import StageWaterfall from '../components/StageWaterfall.vue'
import { fetchWaterfall } from '../services/consoleTraces'
import {
  fetchObservability,
  fetchRecentTraces,
  observabilityQueryKey,
  recentTracesQueryKey,
  type RecentTrace,
} from '../services/traceService'
import type { RetrievalWaterfall } from '../types/traceWaterfall'
import { formatDateTime } from '../utils/datetime'
import { errorText } from '../utils/errorText'
import { describeSource } from '../utils/waterfall'

const limit = ref(20)

const tracesQuery = useQuery({
  queryKey: computed(() => recentTracesQueryKey(limit.value)),
  queryFn: () => fetchRecentTraces(limit.value),
})

const observabilityQuery = useQuery({
  queryKey: computed(() => observabilityQueryKey()),
  queryFn: fetchObservability,
})

const rows = computed(() => tracesQuery.data.value?.items ?? [])
const loading = computed(() => tracesQuery.isPending.value)
const loadError = computed(() =>
  tracesQuery.isError.value ? errorText(tracesQuery.error.value) : '',
)

const langfuseHost = computed(() => observabilityQuery.data.value?.langfuse_host ?? null)

// —— 单条瀑布 ——
const openLogId = ref<string | null>(null)
const waterfall = ref<RetrievalWaterfall | null>(null)
const waterfallLoading = ref(false)
const waterfallError = ref<string | null>(null)

async function openWaterfall(row: RecentTrace): Promise<void> {
  openLogId.value = row.log_id
  waterfall.value = null
  waterfallError.value = null
  waterfallLoading.value = true
  try {
    waterfall.value = await fetchWaterfall(row.log_id)
  } catch (error) {
    waterfallError.value = errorText(error)
  } finally {
    waterfallLoading.value = false
  }
}

function closeWaterfall(): void {
  openLogId.value = null
  waterfall.value = null
  waterfallError.value = null
}

/**
 * 观测栈里那条 trace 的地址。
 *
 * Langfuse v4 的路径形状是 `/project/<projectId>/traces/<traceId>`；`projectId` 我们拿不到
 * （它是栈里的项目 id，不是我们的租户），所以用 `/traces/<traceId>` 这条能落到「按 id 打开」
 * 的路径——v4 会把找不到项目的地址重定向到默认项目。
 */
function traceUrl(traceId: string): string | null {
  const host = langfuseHost.value
  if (host === null || host === '') return null
  return `${host}/traces/${traceId}`
}

function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 8)}…` : value
}

/** 结局一列：降级与空结果是两件事，各有各的说法；都没有才算正常。 */
function outcomeOf(row: RecentTrace): string {
  if (row.degraded_reason) return `降级：${row.degraded_reason}`
  if (row.empty_reason) return `空结果：${row.empty_reason}`
  return '正常'
}

function isAbnormal(row: RecentTrace): boolean {
  return Boolean(row.degraded_reason || row.empty_reason)
}

function scoreLabel(row: RecentTrace): string {
  // 未评分是「还没人评」，与「0 分」不是一回事（1~5 的域里没有 0）。
  return row.score === null ? '—' : `${row.score} 分`
}

/**
 * `el-table` 的行插槽把 `row` 声明成它自己的 `DefaultRow`（结构上是 `Record<string, any>`），
 * 所以直接把它传给上面那几个收 `RecentTrace` 的函数会被 TS 拦。这里显式收窄一次：
 * 数据的真实形状由 `fetchRecentTraces` 的返回类型保证，插槽只是丢了类型信息。
 */
function asTrace(row: unknown): RecentTrace {
  return row as RecentTrace
}
</script>

<template>
  <section class="space-y-4">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-lg font-semibold text-slate-800">检索记录</h1>
        <p class="mt-1 text-xs text-slate-500">
          本租户最近 {{ limit }} 次检索。点一行看它的六段耗时；这一页不显示问句与切片正文。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <el-select v-model="limit" size="small" class="w-24">
          <el-option :value="10" label="10 条" />
          <el-option :value="20" label="20 条" />
          <el-option :value="50" label="50 条" />
          <el-option :value="100" label="100 条" />
        </el-select>
        <el-button size="small" :loading="loading" @click="tracesQuery.refetch()">
          刷新
        </el-button>
      </div>
    </div>

    <p
      v-if="loadError !== ''"
      class="rounded-control border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700"
    >
      {{ loadError }}
    </p>

    <el-card shadow="never" class="rounded-card border border-line">
      <el-table
        v-loading="loading"
        :data="rows"
        size="small"
        empty-text="还没有检索记录。去「检索调试台」发一次检索，这里就会出现一行。"
        @row-click="(row: unknown) => openWaterfall(asTrace(row))"
      >
        <el-table-column label="时间" min-width="150">
          <template #default="{ row }">
            <span class="text-[12px] text-slate-600">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="trace_id" min-width="130">
          <template #default="{ row }">
            <span class="font-mono text-[11px] text-slate-500" :title="row.trace_id">
              {{ shortId(row.trace_id) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">
            <span class="text-[12px] text-slate-600">
              {{ row.latency_ms === null ? '—' : `${row.latency_ms} ms` }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="库数" width="70">
          <template #default="{ row }">
            <span class="text-[12px] text-slate-600">{{ row.kb_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结局" min-width="170">
          <template #default="{ row }">
            <span
              class="text-[12px]"
              :class="isAbnormal(asTrace(row)) ? 'text-[#B45309]' : 'text-slate-600'"
            >
              {{ outcomeOf(asTrace(row)) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="评分" width="80">
          <template #default="{ row }">
            <span class="text-[12px]" :class="row.score === null ? 'text-slate-400' : 'text-slate-700'">
              {{ scoreLabel(asTrace(row)) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="" width="110">
          <template #default="{ row }">
            <a
              v-if="traceUrl(row.trace_id) !== null"
              :href="traceUrl(row.trace_id)!"
              target="_blank"
              rel="noopener noreferrer"
              class="text-[11px] text-brand underline"
              @click.stop
            >
              在观测栈里打开
            </a>
            <span v-else class="text-[11px] text-slate-400" title="当前观测去向不是 langfuse">
              —
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!--
      瀑布弹窗：与检索页那块是同一个组件、同一个端点（先本地记录、后观测服务）。
      这里不复制一套读数逻辑——两份迟早对不上。
    -->
    <el-dialog
      :model-value="openLogId !== null"
      title="六段耗时"
      width="760px"
      @update:model-value="closeWaterfall"
    >
      <p v-if="openLogId !== null" class="mb-2 font-mono text-[11px] text-slate-500">
        log_id {{ openLogId }}
      </p>
      <p v-if="waterfallError !== null" class="text-[11px] text-red-700">
        {{ waterfallError }}
      </p>
      <div v-else-if="waterfallLoading" class="text-[11px] text-muted">读取中…</div>
      <template v-else-if="waterfall !== null">
        <p class="text-[11px] text-muted">
          来源：{{ describeSource(waterfall.source) }}
          <span v-if="waterfall.note">· {{ waterfall.note }}</span>
        </p>
        <StageWaterfall v-if="waterfall.stages.length > 0" class="mt-3" :data="waterfall" />
        <p v-else class="mt-2 text-[11px] text-muted">
          这条记录没有六段耗时（观测没开，或这一条没记上）。
        </p>
      </template>
    </el-dialog>
  </section>
</template>
