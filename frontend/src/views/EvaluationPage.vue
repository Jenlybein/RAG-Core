<script setup lang="ts">
/**
 * 语义评测（#109）：用 LLM 裁判跑固定 50 题，给出 CP / CR。
 *
 * **它只提示、不阻断**。门禁是另一套确定性口径（ragas ID 匹配、零 LLM、同配置两跑一致），
 * 那一份在 `eval/baseline/` 里；这里的分数有裁判随机性，换模型即换尺子，因此不进发布闸门。
 *
 * 为什么从调试台独立出来：它是「整批题库」的评测，与「查一次问题」是两件事。挂在检索页
 * 右栏时，它既被挤进 380px 宽，又和每一次检索的上下文混在一起——而它的结论按「轮」成立，
 * 不按「这一次检索」成立。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import PageHeader from '../components/PageHeader.vue'
import { renderMarkdown } from '../utils/markdown'
import { formatPercent } from '../utils/format'
import { errorText } from '../utils/errorText'
import {
  fetchSideEvidence,
  fetchSideEvidenceReport,
  fetchSideEvidenceRuns,
  triggerSideEvidence,
  type SideEvidenceRun,
} from '../services/sideEvidenceService'

/**
 * 触发一次旁证跑批并轮询状态。
 *
 * 三条约束来自后端契约（`app/api/routes/console_eval.py`）：
 * 1. 触发**只投递任务**、立刻回 `run_id`——跑批是分钟级的，挂在请求上必然超时；
 * 2. `queued` 是正常状态（任务还在队列里），不是「没有这次跑批」；
 * 3. `partial` 也是终态：裁判配额/超时会让部分题没评上，那**不是**检索退化，
 *    所以界面上不把它读成「失败」，只是如实显示已评分/未评分数。
 */
const sideEvidenceRunId = ref<string | null>(null)
const sideEvidenceStatus = ref<string>('')
/**
 * 跑批进度（`{done, total}`）。跑批是分钟级偏上的（一轮上千次裁判请求），没有它界面
 * 只能显示一个没有尽头的「running」。结束/未开始时为 `null`——那时进度条没有意义。
 */
const sideEvidenceProgress = ref<{ done: number; total: number } | null>(null)
const sideEvidenceJudgeModel = ref<string | null>(null)
const sideEvidenceSummary = ref<{
  context_precision?: number | null
  context_recall?: number | null
  count?: number
  scored?: number
  unscored?: number
} | null>(null)
/** 触发/轮询这条路自己出的错（网络、权限、任务投递失败）——「没跑起来」。 */
const sideEvidenceError = ref<string | null>(null)
/** 跑批本身跑完但判了 failed/partial 时，``status.json`` 里写的那个 ``error``——「跑了但没跑好」。 */
const sideEvidenceRunError = ref<string | null>(null)
const sideEvidenceStarting = ref(false)
let sideEvidenceTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 「投递出去多久了」——只为了说出那句「队列里没人消费」。
 *
 * 为什么需要它：触发成功只代表**任务进了队列**，不代表有人跑。只起 API 不起 Worker 时，
 * 状态会永远停在 `queued`，而界面上「正在排队」和「根本没人跑」长得一模一样。前端无法
 * 直接问「有没有 Worker」，但可以按时间说：`queued` 超过 45 秒，基本就不是排队了。
 */
const sideEvidenceQueuedSince = ref<number | null>(null)
const sideEvidenceNow = ref(Date.now())
let sideEvidenceClock: ReturnType<typeof setInterval> | null = null

const SIDE_EVIDENCE_QUEUE_HINT_SECONDS = 45

const sideEvidenceQueuedFor = computed(() => {
  if (sideEvidenceStatus.value !== 'queued' || sideEvidenceQueuedSince.value === null) return 0
  return Math.max(0, Math.round((sideEvidenceNow.value - sideEvidenceQueuedSince.value) / 1000))
})

const sideEvidenceQueueHint = computed(() => {
  if (sideEvidenceQueuedFor.value < SIDE_EVIDENCE_QUEUE_HINT_SECONDS) return ''
  return `已排队 ${sideEvidenceQueuedFor.value} 秒还没开始跑：多半是队列里没有 Worker 在消费（检查 worker 容器 / celery 进程）。`
})

/** 历史跑批：刷新页面后仍能看到「上次那轮」的结果，而不是只剩一片空白。 */
const sideEvidenceRuns = ref<SideEvidenceRun[]>([])
const sideEvidenceRunsError = ref<string | null>(null)

async function loadSideEvidenceRuns(): Promise<void> {
  try {
    sideEvidenceRuns.value = (await fetchSideEvidenceRuns(10)).items
    sideEvidenceRunsError.value = null
  } catch (error) {
    sideEvidenceRunsError.value = errorText(error)
  }
}

/** 报告弹窗：内容由后端按 `run_id` 读出来，前端不拼文件路径。 */
const reportRunId = ref<string | null>(null)
const reportText = ref<string | null>(null)
const reportLoading = ref(false)
const reportError = ref<string | null>(null)

async function openReport(runId: string): Promise<void> {
  reportRunId.value = runId
  reportText.value = null
  reportError.value = null
  reportLoading.value = true
  try {
    const payload = await fetchSideEvidenceReport(runId)
    reportText.value = payload.report
  } catch (error) {
    reportError.value = errorText(error)
  } finally {
    reportLoading.value = false
  }
}

function closeReport(): void {
  reportRunId.value = null
  reportText.value = null
  reportError.value = null
}

/**
 * 报告正文渲染成 HTML。渲染器关掉了原始 HTML（`html: false`），所以 `v-html` 不需要
 * 再来一层 sanitizer——这条前提有测试钉着（`utils/__tests__/markdown.test.ts`）。
 */
const renderedReport = computed(() => renderMarkdown(reportText.value))

const SIDE_EVIDENCE_TERMINAL = new Set(['ok', 'partial', 'failed'])

function stopSideEvidencePolling(): void {
  if (sideEvidenceTimer !== null) {
    clearTimeout(sideEvidenceTimer)
    sideEvidenceTimer = null
  }
}

async function pollSideEvidence(runId: string): Promise<void> {
  try {
    const state = await fetchSideEvidence(runId)
    if (sideEvidenceRunId.value !== runId) return
    sideEvidenceStatus.value = state.status
    const progress = state.progress
    sideEvidenceProgress.value =
      progress && typeof progress.done === 'number' && typeof progress.total === 'number'
        ? { done: progress.done, total: progress.total }
        : null
    sideEvidenceJudgeModel.value = state.judge_model ?? null
    sideEvidenceSummary.value = state.summary ?? null
    sideEvidenceRunError.value = state.error ?? null
    if (SIDE_EVIDENCE_TERMINAL.has(state.status)) {
      stopSideEvidencePolling()
      // 跑完就刷一次历史：刚这轮应当立刻出现在列表里，不需要用户手动刷新。
      void loadSideEvidenceRuns()
      return
    }
  } catch (error) {
    sideEvidenceError.value = errorText(error)
    stopSideEvidencePolling()
    return
  }
  // 3 秒一轮：跑批本身是分钟级，再密只是白打接口；页面离开时由 onUnmounted 收尾。
  sideEvidenceTimer = setTimeout(() => void pollSideEvidence(runId), 3000)
}

async function startSideEvidence(): Promise<void> {
  if (sideEvidenceStarting.value) return
  sideEvidenceStarting.value = true
  sideEvidenceError.value = null
  sideEvidenceRunError.value = null
  stopSideEvidencePolling()
  try {
    const started = await triggerSideEvidence()
    sideEvidenceRunId.value = started.run_id
    sideEvidenceStatus.value = started.status
    sideEvidenceSummary.value = null
    sideEvidenceJudgeModel.value = null
    // 记下投递时刻：`queued` 停留过久时要能说出「队列里没人消费」。
    sideEvidenceQueuedSince.value = Date.now()
    sideEvidenceNow.value = Date.now()
    void pollSideEvidence(started.run_id)
  } catch (error) {
    sideEvidenceError.value = errorText(error)
  } finally {
    sideEvidenceStarting.value = false
  }
}

onMounted(() => {
  void loadSideEvidenceRuns()
  // 每秒推进一次「已排队多久」：只在 queued 时才有人读它，但计时器本身开销可以忽略，
  // 比「按需启停」少一条要维护的状态。
  sideEvidenceClock = setInterval(() => {
    sideEvidenceNow.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  stopSideEvidencePolling()
  if (sideEvidenceClock !== null) {
    clearInterval(sideEvidenceClock)
    sideEvidenceClock = null
  }
})

</script>

<template>
  <section class="space-y-6">
    <PageHeader
      title="语义评测"
      subtitle="模型裁判跑固定 50 题，出 CP / CR。只提示、不阻断——门禁另有确定性口径。"
    >
      <template #actions>
        <el-button
          type="primary"
          :loading="sideEvidenceStarting"
          :disabled="sideEvidenceStatus === 'running' || sideEvidenceStatus === 'queued'"
          @click="startSideEvidence"
        >
          触发一次评测
        </el-button>
      </template>
    </PageHeader>

    <div class="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_400px]">
      <!-- 当前这一轮 -->
      <section class="card card-body">
        <h2 class="section-title">当前这一轮</h2>

        <p v-if="sideEvidenceError !== null" class="mt-3 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {{ sideEvidenceError }}
        </p>

        <p v-else-if="sideEvidenceRunId === null" class="mt-3 text-sm text-muted">
          还没有触发过。点右上角「触发一次评测」跑一轮（约 2~3 分钟，取决于裁判速度）。
        </p>

        <div v-else class="mt-3 space-y-3">
          <div class="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm">
            <span class="font-mono text-muted">{{ sideEvidenceRunId }}</span>
            <span class="font-medium text-ink">{{ sideEvidenceStatus }}</span>
            <el-button
              v-if="SIDE_EVIDENCE_TERMINAL.has(sideEvidenceStatus)"
              link
              type="primary"
              size="small"
              @click="openReport(sideEvidenceRunId!)"
            >
              看报告
            </el-button>
          </div>

          <p v-if="sideEvidenceJudgeModel" class="text-sm text-muted">
            裁判模型 <span class="font-mono text-ink">{{ sideEvidenceJudgeModel }}</span>
            —— 换模型 = 换尺子，与旧报告不可比。
          </p>

          <div v-if="sideEvidenceStatus === 'running' && sideEvidenceProgress !== null" class="space-y-1.5">
            <p class="text-sm text-muted">
              已完成 {{ sideEvidenceProgress.done }} / {{ sideEvidenceProgress.total }} 题
            </p>
            <el-progress
              :percentage="Math.round((sideEvidenceProgress.done / Math.max(1, sideEvidenceProgress.total)) * 100)"
              :stroke-width="6"
              :show-text="false"
            />
          </div>

          <!-- 分数块：数字放大，分母写在旁边 -->
          <div v-if="sideEvidenceSummary" class="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <p class="metric-value">{{ formatPercent(sideEvidenceSummary.context_precision) }}</p>
              <p class="metric-label">CP 上下文精确率</p>
            </div>
            <div>
              <p class="metric-value">{{ formatPercent(sideEvidenceSummary.context_recall) }}</p>
              <p class="metric-label">CR 上下文召回率</p>
            </div>
            <div>
              <p class="metric-value">{{ sideEvidenceSummary.scored ?? '—' }}</p>
              <p class="metric-label">已评分</p>
            </div>
            <div>
              <p class="metric-value">{{ sideEvidenceSummary.unscored ?? '—' }}</p>
              <p class="metric-label">未评分</p>
            </div>
          </div>
          <p v-if="sideEvidenceSummary?.scored !== undefined" class="text-xs text-muted">
            CP / CR 的分母是「已评分」那一列，不是题目总数。
          </p>

          <p v-if="sideEvidenceRunError" class="rounded-control border border-[#f0dfae] bg-[#fff8e6] px-3 py-2 text-xs text-[#7a5b00]">
            {{ sideEvidenceRunError }}
          </p>
          <p v-if="sideEvidenceQueueHint" class="text-xs text-[#7a5b00]">{{ sideEvidenceQueueHint }}</p>
        </div>
      </section>

      <!-- 最近几轮 -->
      <section class="card card-body">
        <div class="flex items-baseline justify-between gap-2">
          <h2 class="section-title">最近几轮</h2>
          <el-button link size="small" @click="loadSideEvidenceRuns">刷新</el-button>
        </div>

        <p v-if="sideEvidenceRunsError !== null" class="mt-2 text-xs text-red-700">
          {{ sideEvidenceRunsError }}
        </p>
        <p v-else-if="sideEvidenceRuns.length === 0" class="mt-2 text-sm text-muted">
          还没有跑批记录。
        </p>
        <ul v-else class="mt-3 divide-y divide-line">
          <li v-for="run in sideEvidenceRuns" :key="run.run_id" class="py-2.5">
            <div class="flex flex-wrap items-baseline justify-between gap-2">
              <span class="font-mono text-xs text-muted">{{ run.run_id }}</span>
              <span class="text-xs font-medium text-ink">{{ run.status }}</span>
            </div>
            <div class="mt-1 flex flex-wrap items-baseline justify-between gap-2">
              <span v-if="run.summary" class="text-xs text-muted">
                CP {{ formatPercent(run.summary.context_precision) }} ·
                CR {{ formatPercent(run.summary.context_recall) }} ·
                {{ run.summary.scored }}/{{ run.summary.unscored }}
              </span>
              <span v-else class="text-xs text-muted">无分数</span>
              <el-button v-if="run.has_report" link size="small" @click="openReport(run.run_id)">
                看报告
              </el-button>
            </div>
          </li>
        </ul>
      </section>
    </div>

    <!-- 报告弹窗 -->
    <el-dialog
      append-to-body
      :model-value="reportRunId !== null"
      title="评测报告"
      width="880px"
      @update:model-value="closeReport"
    >
      <p v-if="reportRunId !== null" class="mb-2 font-mono text-xs text-muted">
        run {{ reportRunId }}
      </p>
      <p v-if="reportError !== null" class="text-sm text-red-700">{{ reportError }}</p>
      <p v-else-if="reportLoading" class="text-sm text-muted">读取中…</p>
      <p v-else-if="reportText === null" class="text-sm text-muted">
        这一轮没有报告文件（可能还在排队，或中途失败没落地）。
      </p>
      <div v-else class="max-h-[60vh] overflow-auto rounded-control bg-surface px-4 py-3" v-html="renderedReport" />
    </el-dialog>
  </section>
</template>
