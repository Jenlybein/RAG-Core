<script setup lang="ts">
/**
 * 对比面板：把「当前这次」与基准那次并排逐字段 diff。
 *
 * 面板只做展示与选择，所有判定（历史裁剪、摘要 diff、结果配对、基准取自哪一条）都在
 * `utils/retrieveCompare.ts` 的纯函数里；组件不自己算 diff，否则「null 不是 0」这类
 * 契约细节会在模板里慢慢漂掉。
 *
 * 历史**只在内存**：对比是排障时「刚刚改了什么」的会话内手段，不是持久化报表。
 * 刷新 / 退出即清空，不落 localStorage/sessionStorage —— 上一次会话的基准拿来对比
 * 新的响应，只会让人误以为配置没变。
 */
import { computed } from 'vue'

import type { CompareHistoryEntry } from '../utils/retrieveCompare'
import {
  HISTORY_LIMIT,
  diffResults,
  diffSummary,
  isLatest,
  resolveBase,
  type DiffDirection,
  type ResultFieldName,
} from '../utils/retrieveCompare'
import { formatDateTime } from '../utils/datetime'
import { formatRank, formatScore } from '../utils/format'

const props = defineProps<{
  /** 会话内成功的检索记录，按时间从旧到新；上限由 `pushHistory` 保证。 */
  history: CompareHistoryEntry[]
  /** 钉住的「第 N 次」；null 表示没钉，默认对比上一次。 */
  pinnedSeq: number | null
}>()

const emit = defineEmits<{
  pin: [seq: number]
  unpin: []
}>()

const latest = computed<CompareHistoryEntry | null>(
  () => props.history[props.history.length - 1] ?? null,
)
const base = computed(() => resolveBase(props.history, props.pinnedSeq))
/** 当前用的基准是不是「用户钉住的那条」，而不是「上一次」兜底。 */
const pinnedApplied = computed(
  () => base.value !== null && props.pinnedSeq !== null && base.value.seq === props.pinnedSeq,
)
/**
 * 钉住的那条是否还在 5 次窗口里。
 *
 * 不在时要明说：静默回落到「上一次」会让用户以为还在跟钉住的那次比，
 * 而这恰好是配置排查里最容易得出错误结论的地方。
 */
const pinnedPresent = computed(
  () => props.pinnedSeq !== null && props.history.some((entry) => entry.seq === props.pinnedSeq),
)

const summaryRows = computed(() => {
  if (base.value === null || latest.value === null) return []
  return diffSummary(base.value.data, latest.value.data)
})

const resultDiff = computed(() => {
  if (base.value === null || latest.value === null) return null
  return diffResults(base.value.data, latest.value.data)
})

const diffCount = computed(() => {
  const diff = resultDiff.value
  if (diff === null) return '没有可比的结果'
  return `新增 ${diff.added.length} · 消失 ${diff.removed.length} · 变化 ${diff.changed.length} · 其余 ${diff.unchanged} 条一致`
})

/** 朝上 / 朝前移；`appear` 与 `vanish` 不是普通涨跌，单独给符号。 */
const DIRECTION_MARK: Record<DiffDirection, string> = {
  up: '↑',
  down: '↓',
  appear: '+',
  vanish: '−',
  none: '=',
}

function directionMark(direction: DiffDirection): string {
  return DIRECTION_MARK[direction]
}

function directionText(direction: DiffDirection): string {
  if (direction === 'up') return '变好 / 前移'
  if (direction === 'down') return '变差 / 后移'
  if (direction === 'appear') return '这次命中（之前是「—」）'
  if (direction === 'vanish') return '这次没命中（之前有值）'
  return '没变'
}

/** up 用主色、down 用危险色；数值本身不承担语义，颜色与箭头一起说。 */
function directionClass(direction: DiffDirection): string {
  if (direction === 'down' || direction === 'vanish') return 'text-danger'
  if (direction === 'up' || direction === 'appear') return 'text-brand'
  return 'text-slate-500'
}

function directionCellClass(direction: DiffDirection): string {
  if (direction === 'down' || direction === 'vanish') return 'bg-danger/5'
  if (direction === 'up' || direction === 'appear') return 'bg-brand/5'
  return ''
}

/** 分数按 4 位；两路名次与列表位次用「第 N 名」，与检索页同一套格式。 */
function fieldValue(field: ResultFieldName, value: number | null): string {
  return field === 'score' || field === 'rrf_score' ? formatScore(value) : formatRank(value)
}
</script>

<template>
  <el-card v-if="history.length > 0" shadow="never">
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 class="text-sm font-semibold text-slate-800">对比面板（会话内）</h2>
          <p class="mt-1 text-xs text-slate-500">
            左右两列分别是「基准」与「当前」；有值的差异才上色。历史只留在内存，刷新或退出页面即清空
            —— 对比是会话内的调试手段，不是持久化报表。
          </p>
        </div>
        <span v-if="latest !== null" class="shrink-0 text-xs text-slate-500">
          当前：第 {{ latest.seq }} 次 · {{ formatDateTime(latest.at) }}
        </span>
      </div>
    </template>

    <div class="space-y-4">
      <!-- 最近几次：列表本身就是「第 N 次 + 时间」的出处 -->
      <section>
        <h3 class="text-xs font-semibold text-slate-600">
          最近几次检索（最多 {{ HISTORY_LIMIT }} 条；点「钉为基准」把某一次固定成参照）
        </h3>
        <div class="mt-2 flex flex-wrap gap-2">
          <div
            v-for="entry in history"
            :key="entry.seq"
            class="min-w-[15rem] rounded border px-3 py-2 text-xs"
            :class="
              isLatest(history, entry.seq)
                ? 'border-brand bg-brand/5'
                : 'border-line bg-white'
            "
          >
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span class="font-medium text-slate-700">第 {{ entry.seq }} 次</span>
              <span class="text-slate-400">{{ formatDateTime(entry.at) }}</span>
              <span
                v-if="isLatest(history, entry.seq)"
                class="rounded bg-white px-1 text-xs text-brand"
              >
                当前
              </span>
              <span
                v-if="pinnedSeq === entry.seq"
                class="rounded px-1 text-xs text-white"
                style="background-color: #045b89"
              >
                基准
              </span>
            </div>
            <p class="mt-1 break-all text-slate-500">
              <span class="font-mono">{{ entry.data.metadata.mode }}</span>
              · top_k {{ entry.data.metadata.top_k }} · {{ entry.data.retrieved_chunks.length }} 条 ·
              {{ entry.data.query }}
            </p>
            <div class="mt-2">
              <el-button v-if="pinnedSeq !== entry.seq" size="small" @click="emit('pin', entry.seq)">
                钉为基准
              </el-button>
              <el-button v-else size="small" @click="emit('unpin')">取消钉基准</el-button>
            </div>
          </div>
        </div>

        <p
          v-if="pinnedSeq !== null && !pinnedPresent"
          class="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
        >
          钉住的第 {{ pinnedSeq }} 次已经超出 5 次窗口，暂时按「上一次」对比；要固定参照就重新钉一条。
        </p>
      </section>

      <template v-if="base !== null && latest !== null">
        <!-- 顶部标题就是「与第 N 次（时间）对比」 -->
        <section class="border-t border-line pt-3">
          <div class="flex flex-wrap items-center gap-2">
            <h3 class="text-sm font-semibold text-slate-800">
              与第 {{ base.seq }} 次（{{ formatDateTime(base.at) }}）对比
            </h3>
            <span
              v-if="pinnedApplied"
              class="rounded px-1.5 py-0.5 text-xs text-white"
              style="background-color: #045b89"
            >
              已钉为基准
            </span>
            <span v-else-if="pinnedSeq !== null" class="text-xs text-slate-500">
              已钉住第 {{ pinnedSeq }} 次，下一次检索会自动与它对比
            </span>
          </div>

          <!-- 运行摘要：两列并排，变化项整行上色 -->
          <div class="mt-3 overflow-x-auto">
            <table class="w-full table-fixed border-collapse text-sm">
              <thead>
                <tr class="border-b border-line text-left text-xs text-slate-400">
                  <th class="w-1/4 py-1 pr-3 font-normal">字段</th>
                  <th class="w-[37.5%] py-1 pr-3 font-normal">基准 · 第 {{ base.seq }} 次</th>
                  <th class="w-[37.5%] py-1 font-normal">当前 · 第 {{ latest.seq }} 次</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in summaryRows"
                  :key="row.field"
                  class="border-b border-line/70 align-top"
                  :class="row.changed ? 'bg-brand/5' : ''"
                >
                  <td class="py-1.5 pr-3">
                    <span class="text-slate-700">{{ row.label }}</span>
                    <code class="ml-1 rounded bg-slate-100 px-1 font-mono text-xs text-slate-400">
                      {{ row.field }}
                    </code>
                  </td>
                  <td
                    class="break-all py-1.5 pr-3 font-mono"
                    :class="row.changed ? 'text-slate-400' : 'text-slate-700'"
                  >
                    {{ row.before }}
                  </td>
                  <td
                    class="break-all py-1.5 font-mono"
                    :class="row.changed ? 'font-semibold text-brand' : 'text-slate-700'"
                  >
                    {{ row.after }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="mt-1 text-xs text-slate-400">
            没变的行也留着：只列变化项时，「没变」与「这一字段没取到」在界面上无法区分。
          </p>

          <!-- 结果集 diff -->
          <div class="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
            <h4 class="text-sm font-semibold text-slate-800">召回结果的变化</h4>
            <span class="text-xs text-slate-500">{{ diffCount }}</span>
          </div>

          <template v-if="resultDiff !== null">
            <p
              v-if="
                resultDiff.added.length === 0 &&
                resultDiff.removed.length === 0 &&
                resultDiff.changed.length === 0
              "
              class="mt-2 rounded border border-line bg-slate-50 px-3 py-2 text-sm text-slate-600"
            >
              这两次的结果集完全一致（{{ resultDiff.unchanged }} 条）：分数、名次与位次都没动。
            </p>

            <div v-if="resultDiff.added.length > 0" class="mt-3">
              <h5 class="flex items-center gap-2 text-xs font-semibold">
                <span
                  class="rounded px-1.5 py-0.5 text-xs text-white"
                  style="background-color: #045b89"
                >
                  新增 {{ resultDiff.added.length }} 条
                </span>
                <span class="font-normal text-slate-400">当前这次有、基准没有</span>
              </h5>
              <ul class="mt-1 divide-y divide-line">
                <li
                  v-for="item in resultDiff.added"
                  :key="item.key"
                  class="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-1.5 text-sm"
                >
                  <span class="shrink-0 rounded bg-brand/10 px-1.5 text-xs text-brand">
                    {{ formatRank(item.rank) }}
                  </span>
                  <span class="text-slate-800">{{ item.title || '（无标题）' }}</span>
                  <span class="font-mono text-xs text-slate-500">分 {{ formatScore(item.score) }}</span>
                  <span class="font-mono text-xs text-slate-400">{{ item.chunk_id }}</span>
                  <span v-if="item.kb_id !== null" class="text-xs text-slate-400">
                    库：<span class="font-mono">{{ item.kb_id }}</span>
                  </span>
                </li>
              </ul>
            </div>

            <div v-if="resultDiff.removed.length > 0" class="mt-3">
              <h5 class="flex items-center gap-2 text-xs font-semibold">
                <span
                  class="rounded px-1.5 py-0.5 text-xs text-white"
                  style="background-color: #fa5252"
                >
                  消失 {{ resultDiff.removed.length }} 条
                </span>
                <span class="font-normal text-slate-400">基准有、当前这次没有</span>
              </h5>
              <ul class="mt-1 divide-y divide-line">
                <li
                  v-for="item in resultDiff.removed"
                  :key="item.key"
                  class="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-1.5 text-sm"
                >
                  <span class="shrink-0 rounded bg-danger/10 px-1.5 text-xs text-danger">
                    基准 {{ formatRank(item.rank) }}
                  </span>
                  <span class="text-slate-800">{{ item.title || '（无标题）' }}</span>
                  <span class="font-mono text-xs text-slate-500">分 {{ formatScore(item.score) }}</span>
                  <span class="font-mono text-xs text-slate-400">{{ item.chunk_id }}</span>
                  <span v-if="item.kb_id !== null" class="text-xs text-slate-400">
                    库：<span class="font-mono">{{ item.kb_id }}</span>
                  </span>
                </li>
              </ul>
            </div>

            <div v-if="resultDiff.changed.length > 0" class="mt-3">
              <h5 class="text-xs font-semibold text-slate-600">
                变化 {{ resultDiff.changed.length }} 条（两边都有，但分数 / 名次 / 位次不同）
              </h5>
              <ul class="mt-1 divide-y divide-line">
                <li v-for="item in resultDiff.changed" :key="item.key" class="py-2">
                  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
                    <span class="text-slate-800">{{ item.title || '（无标题）' }}</span>
                    <span class="font-mono text-xs text-slate-400">{{ item.chunk_id }}</span>
                    <span v-if="item.kb_id !== null" class="text-xs text-slate-400">
                      库：<span class="font-mono">{{ item.kb_id }}</span>
                    </span>
                  </div>
                  <table class="mt-1 w-full table-fixed border-collapse text-xs">
                    <thead>
                      <tr class="text-left text-slate-400">
                        <th class="w-1/4 py-0.5 pr-2 font-normal">字段</th>
                        <th class="w-[30%] py-0.5 pr-2 font-normal">基准</th>
                        <th class="w-[10%] py-0.5 pr-2 font-normal">方向</th>
                        <th class="w-[35%] py-0.5 font-normal">当前</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="field in item.fields"
                        :key="field.field"
                        :class="directionCellClass(field.direction)"
                      >
                        <td class="py-0.5 pr-2 text-slate-600">{{ field.label }}</td>
                        <td
                          class="break-all py-0.5 pr-2 font-mono"
                          :class="field.changed ? 'text-slate-400' : 'text-slate-700'"
                        >
                          {{ fieldValue(field.field, field.before) }}
                        </td>
                        <td
                          class="py-0.5 pr-2 font-mono"
                          :class="directionClass(field.direction)"
                        >
                          <el-tooltip
                            v-if="field.changed"
                            :content="directionText(field.direction)"
                            placement="top"
                          >
                            <span class="cursor-help">{{ directionMark(field.direction) }}</span>
                          </el-tooltip>
                          <span v-else class="text-slate-300">=</span>
                        </td>
                        <td
                          class="break-all py-0.5 font-mono"
                          :class="directionClass(field.direction)"
                        >
                          {{ fieldValue(field.field, field.after) }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </li>
              </ul>
            </div>
          </template>
        </section>
      </template>

      <p v-else class="rounded border border-line bg-slate-50 px-3 py-2 text-sm text-slate-600">
        再跑一次检索，这里就会自动拿「上一次」做对照；把某一次钉为基准后，之后每一次都会跟它比。
      </p>
    </div>
  </el-card>
</template>
