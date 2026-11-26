<script setup lang="ts">
/**
 * 套餐管理页：列出全部套餐、编辑一档。
 *
 * 三处刻意的实现，都是票面点名的：
 *
 * 1. **保存是两步**：先 `PATCH ?dry_run=true` 拿影响面，若有影响再弹确认，用户点「确认」
 *    才发 `dry_run=false` 的真写请求；点取消则什么都不落库。不能只发一次真写——那样
 *    影响面只能在事后从审计里看到，而客户已经当场超限。
 * 2. **影响面的数字用 `count`**：后端 `sample` 最多只放 5 个名字，拿 `sample.length`
 *    当条件会在受影响对象更多时漏掉弹窗。判定在 `utils/adminPlans.ts`（有 vitest）。
 * 3. **授权位只勾选、不自由输入**：五个勾选框来自常量，保存时 `buildPlanPatch` 只发
 *    真的改了的位，未知字段不可能发到后端（`extra=forbid` 会判 20002）。
 *
 * **删除入口暂时下线**。后端 `DELETE /console/plans/{plan_id}` 仍在（还被 `20019` 引用
 * 判定守着），但界面上不提供入口：删掉一个套餐的后果是「用它判定的所有租户当场换档」，
 * 而这件事在产品侧还没定清楚怎么确认与回滚。原来那套「从账号列表反推引用、猜不出来就
 * 让后端 20019 兜底」的做法本身就是这个不确定性的体现。真要做，应是一次带影响面预览的
 * 完整流程，而不是把按钮加回来。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, h, reactive, ref } from 'vue'

import PageHeader from '../../components/PageHeader.vue'

import { useDryRunSave } from '../../composables/useDryRunSave'
import {
  PLANS_QUERY_KEY,
  fetchPlans,
  updatePlan,
} from '../../services/consoleAdmin'
import type { PlanData, PlanEditableFields, PlanImpact } from '../../types/consoleAdmin'
import {
  PLAN_CAPABILITIES,
  PLAN_NUMBER_FIELDS,
  PLAN_ZEROABLE_FIELDS,
  buildPlanPatch,
  impactLines,
  shouldConfirm,
} from '../../utils/adminPlans'
import { formatDateTime } from '../../utils/datetime'
import { errorText } from '../../utils/errorText'

const queryClient = useQueryClient()

const plansQuery = useQuery({ queryKey: PLANS_QUERY_KEY, queryFn: fetchPlans })

const plans = computed(() => plansQuery.data.value?.plans ?? [])
const loading = computed(() => plansQuery.isPending.value)
const loadError = computed(() =>
  plansQuery.isError.value ? errorText(plansQuery.error.value) : '',
)

function editableOf(plan: PlanData): PlanEditableFields {
  return {
    name: plan.name,
    retrieve_allowed: plan.retrieve_allowed,
    rewrite_allowed: plan.rewrite_allowed,
    decompose_allowed: plan.decompose_allowed,
    research_allowed: plan.research_allowed,
    rerank_allowed: plan.rerank_allowed,
    retrieve_qps: plan.retrieve_qps,
    retrieve_daily: plan.retrieve_daily,
    rerank_qps: plan.rerank_qps,
    rerank_daily: plan.rerank_daily,
    llm_qps: plan.llm_qps,
    llm_daily: plan.llm_daily,
    max_top_k: plan.max_top_k,
    max_kb: plan.max_kb,
    max_kb_per_retrieve: plan.max_kb_per_retrieve,
    max_documents_per_kb: plan.max_documents_per_kb,
    max_tokens_per_kb: plan.max_tokens_per_kb,
    chunk_daily_tokens: plan.chunk_daily_tokens,
  }
}

function emptyDraft(): PlanEditableFields {
  return {
    name: '',
    retrieve_allowed: true,
    rewrite_allowed: false,
    decompose_allowed: false,
    research_allowed: false,
    rerank_allowed: false,
    retrieve_qps: 1,
    retrieve_daily: 1,
    rerank_qps: 0,
    rerank_daily: 0,
    llm_qps: 0,
    llm_daily: 0,
    max_top_k: 0,
    max_kb: 1,
    max_kb_per_retrieve: 1,
    max_documents_per_kb: 1,
    // 新建草稿的容量初值：给「最小可用」而不是 0——0 会让这个套餐下所有写入当场被拒
    // （容量判据的意义就是「装不装得下」，填 0 等于「什么也装不下」）。
    max_tokens_per_kb: 30_000,
    chunk_daily_tokens: 300_000,
  }
}

// 表单草稿用 reactive 对象而不是 `ref<PlanEditableFields | null>`：对话框可以关，
// 但输入框的 v-model 永远有一个合法对象可绑，模板里不用到处判空。
const draft = reactive<PlanEditableFields>(emptyDraft())
const editingPlan = ref<PlanData | null>(null)
const dialogVisible = ref(false)

/**
 * 两步保存走通用组合式函数：试算 → （有影响才）确认 → 真写。
 *
 * 判定与文案仍留在本页：套餐的 `PlanImpact` 形状（租户超库数 / 超出新授权范围）与检索
 * 参数页的 `SettingsImpact` 不同，确认条件也只有本页能判。组合式函数只负责顺序与 loading。
 */
const { saving, save: runDryRunSave } = useDryRunSave<PlanImpact>({
  formatImpact: (impact) => {
    if (!shouldConfirm(impact)) return null
    return {
      title: '保存前确认影响面',
      message: h('div', { class: 'text-sm leading-6' }, [
        ...impactLines(impact).map((line) => h('p', line)),
        h('p', { class: 'mt-2 text-slate-500' }, '确认后才会真正落库；取消不会写入。'),
      ]),
    }
  },
})

function openEdit(plan: PlanData): void {
  Object.assign(draft, editableOf(plan))
  editingPlan.value = plan
  dialogVisible.value = true
}

/** 这一档开了哪些能力。返回标签数组，空数组表示一个都没开。 */
function enabledCapabilities(plan: PlanData): string[] {
  return PLAN_CAPABILITIES.filter((capability) => plan[capability.key]).map(
    (capability) => capability.label,
  )
}

/**
 * 保存：校验 → 两步保存（试算 → 有影响才确认 → 真写）。
 *
 * 两步顺序与 loading 交给 `useDryRunSave`；本页只负责校验、影响面文案，以及保存成功后
 * 关对话框 / 失效查询缓存。「取消确认」与「请求失败」在组合式函数里已经分开返回，
 * 前者不落库也不弹错误。
 */
async function save(): Promise<void> {
  const plan = editingPlan.value
  if (plan === null) return

  if (draft.name.trim() === '') {
    ElMessage.warning('套餐名不能为空')
    return
  }
  for (const field of PLAN_NUMBER_FIELDS) {
    const value = draft[field.key]
    if (!Number.isInteger(value) || value < 1) {
      ElMessage.warning(`${field.label}必须是 ≥ 1 的整数（单位：${field.unit}）`)
      return
    }
  }
  for (const field of PLAN_ZEROABLE_FIELDS) {
    const value = draft[field.key]
    if (!Number.isInteger(value) || value < 0) {
      ElMessage.warning(`${field.label}必须是 ≥ 0 的整数（0 = 关闭 / 不额外封顶）`)
      return
    }
  }

  const patch = buildPlanPatch(editableOf(plan), draft)
  if (Object.keys(patch).length === 0) {
    ElMessage.info('没有改动，未发送请求')
    return
  }

  // 把这一轮点保存时的 patch 捕进闭包里：用户若在试算途中改了输入框，
  // 确认后发出的真写必须仍是刚刚预览的那一份改动。
  const outcome = await runDryRunSave((dryRun) => updatePlan(plan.plan_id, patch, dryRun))
  if (outcome.status === 'failed') {
    ElMessage.error(errorText(outcome.error))
    return
  }
  if (outcome.status === 'cancelled') return

  ElMessage.success('已保存：下一次读取即按新配置判定')
  dialogVisible.value = false
  await queryClient.invalidateQueries({ queryKey: PLANS_QUERY_KEY })
}

async function refresh(): Promise<void> {
  await plansQuery.refetch()
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader title="套餐管理">
      <template #actions>
        <span v-if="plans.length > 0" class="text-sm text-muted">{{ plans.length }} 档</span>
        <el-button size="small" :loading="plansQuery.isFetching.value" @click="refresh">
          刷新
        </el-button>
      </template>
    </PageHeader>

    <section class="card card-body">
    <div v-if="loading" class="py-10 text-center text-sm text-slate-500">加载套餐…</div>

    <div
      v-else-if="loadError !== ''"
      class="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      <p>套餐列表读取失败：{{ loadError }}</p>
      <el-button class="mt-2" size="small" @click="refresh">重试</el-button>
    </div>

    <div v-else class="mt-5 overflow-x-auto">
      <div v-if="plans.length === 0" class="py-10 text-center">
        <p class="text-[15px] font-semibold text-ink">还没有套餐</p>
      </div>

      <table v-else class="min-w-full text-sm">
        <thead>
          <tr>
            <th class="th-label px-3 py-2 text-left">套餐</th>
            <th class="th-label px-3 py-2 text-left">授权位</th>
            <th class="th-label px-3 py-2 text-left">上限</th>
            <th class="th-label px-3 py-2 text-left">更新时间</th>
            <th class="th-label px-3 py-2 text-left">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in plans" :key="plan.plan_id" class="border-t border-line align-top transition-colors hover:bg-surface-muted/60">
            <td class="px-3 py-3">
              <p class="font-medium text-ink">{{ plan.name }}</p>
              <p class="font-mono text-xs text-muted">{{ plan.code }}</p>
            </td>

            <td class="px-3 py-3">
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="capability in enabledCapabilities(plan)"
                  :key="capability"
                  class="rounded-full bg-[#E4EDDD] px-2 py-0.5 text-xs text-[#3C5228]"
                >
                  {{ capability }}
                </span>
                <span v-if="enabledCapabilities(plan).length === 0" class="text-xs text-slate-400">
                  —
                </span>
              </div>
            </td>

            <!--
              上限原来是六个「标签：值 单位」各占一行，行高被撑到三倍、扫读时像在读数表。
              改成 chips：每个数值自带完整语义（值 + 单位 + 短标签），换行只发生在块之间。
            -->
            <td class="px-3 py-3">
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="field in PLAN_NUMBER_FIELDS"
                  :key="field.key"
                  class="rounded-full border border-line bg-surface-muted px-2 py-0.5 text-xs whitespace-nowrap text-slate-600"
                  :title="field.label"
                >
                  {{ field.short }} {{ plan[field.key] }} {{ field.unit }}
                </span>
                <span
                  v-for="field in PLAN_ZEROABLE_FIELDS"
                  :key="field.key"
                  class="rounded-full border border-line bg-surface-muted px-2 py-0.5 text-xs whitespace-nowrap text-slate-600"
                  :title="field.label"
                >
                  {{ field.short }} {{ plan[field.key] }} {{ field.unit }}
                </span>
              </div>
            </td>

            <td class="px-3 py-3 text-xs whitespace-nowrap text-muted">
              {{ formatDateTime(plan.updated_at) }}
            </td>
            <td class="px-3 py-3">
              <!-- 只有「编辑」：删除入口暂时下线，理由见本文件顶部模块文档。 -->
              <el-button size="small" @click="openEdit(plan)">编辑</el-button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      append-to-body
      title="编辑套餐"
      width="640px"
      :close-on-click-modal="false"
    >
      <div class="space-y-5 text-sm">
        <!-- 套餐标识只读：code 是脚本与日志的指代，改名要连脚本一起动。 -->
        <div class="flex flex-wrap items-baseline gap-2">
          <span class="w-20 shrink-0 text-xs text-muted">套餐标识</span>
          <span class="font-mono text-[13px] text-ink">{{ editingPlan?.code ?? '—' }}</span>
        </div>

        <label class="flex items-center gap-2">
          <span class="w-20 shrink-0 text-xs text-muted">套餐名</span>
          <el-input v-model="draft.name" maxlength="128" />
        </label>

        <div>
          <p class="mb-2 text-xs text-muted">授权位</p>
          <!--
            用勾选框而不是开关：Element 的 switch 关闭态是一块浅粉，压在这套米白上很扎眼，
            而「这个能力开没开」本来就是勾选语义。五个并排也更好一眼扫完。
          -->
          <div class="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
            <el-checkbox
              v-for="capability in PLAN_CAPABILITIES"
              :key="capability.key"
              v-model="draft[capability.key]"
            >
              {{ capability.label }}
            </el-checkbox>
          </div>
        </div>

        <div>
          <p class="mb-2 text-xs text-muted">上限</p>
          <div class="grid gap-3 sm:grid-cols-2">
            <label
              v-for="field in PLAN_NUMBER_FIELDS"
              :key="field.key"
              class="flex items-center gap-2"
            >
              <span class="w-28 shrink-0 text-xs text-muted">{{ field.label }}</span>
              <el-input-number
                v-model="draft[field.key]"
                :min="1"
                :step="1"
                controls-position="right"
                style="width: 96px"
              />
              <span class="text-xs text-muted">{{ field.unit }}</span>
            </label>
          </div>
        </div>

        <div>
          <p class="mb-2 text-xs text-muted">
            加工额度与质量封顶
          </p>
          <div class="grid gap-3 sm:grid-cols-2">
            <label
              v-for="field in PLAN_ZEROABLE_FIELDS"
              :key="field.key"
              class="flex items-center gap-2"
            >
              <span class="w-28 shrink-0 text-xs text-muted">{{ field.label }}</span>
              <el-input-number
                v-model="draft[field.key]"
                :min="0"
                :step="1"
                controls-position="right"
                style="width: 96px"
              />
              <span class="text-xs text-muted">{{ field.unit }}</span>
            </label>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
    </section>
  </section>
</template>
