<script setup lang="ts">
/**
 * 套餐管理页：列出全部套餐、编辑一档、删除未被引用的套餐。
 *
 * 三处刻意的实现，都是票面点名的：
 *
 * 1. **保存是两步**：先 `PATCH ?dry_run=true` 拿影响面，若有影响再弹确认，用户点「确认」
 *    才发 `dry_run=false` 的真写请求；点取消则什么都不落库。不能只发一次真写——那样
 *    影响面只能在事后从审计里看到，而客户已经当场超限。
 * 2. **影响面的数字用 `count`**：后端 `sample` 最多只放 5 个名字，拿 `sample.length`
 *    当条件会在受影响对象更多时漏掉弹窗。判定在 `utils/adminPlans.ts`（有 vitest）。
 * 3. **档位只勾选、不自由输入**：四个勾选框来自常量，保存前再用 `buildPlanPatch` 过滤
 *    一遍，未知值不可能发到后端。
 *
 * 删除按钮的显示依赖「引用信息」：`GET /console/plans` 的 `PlanData` **没有**引用计数/租户
 * 列表字段，所以只能从账号列表（`account.plan.plan_id`）反推出可见的引用关系。这个推断
 * 有一个已知盲区：一个租户若没有任何账号，它引用的套餐在这里看起来是「未引用」的，点删除
 * 会被后端的 `20019` 拒绝并给出提示。宁可这样，也不要为了一个不存在的接口去猜。
 * 反过来，引用信息没读到时**一个删除入口都不给**：删除不可撤销，不能拿「大概没被引用」去赌。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, h, reactive, ref } from 'vue'

import { useDryRunSave } from '../../composables/useDryRunSave'
import {
  ACCOUNTS_QUERY_KEY,
  PLANS_QUERY_KEY,
  deletePlan,
  fetchAccounts,
  fetchPlans,
  updatePlan,
} from '../../services/consoleAdmin'
import type {
  PlanData,
  PlanEditableFields,
  PlanImpact,
  PlanNumericField,
} from '../../types/consoleAdmin'
import { KEY_PROFILES } from '../../types/key'
import {
  PLAN_CAPABILITIES,
  PLAN_NUMBER_FIELDS,
  buildPlanPatch,
  impactLines,
  pickKnownProfiles,
  shouldConfirm,
} from '../../utils/adminPlans'
import { formatDateTime } from '../../utils/datetime'
import { errorText } from '../../utils/errorText'

const queryClient = useQueryClient()

const plansQuery = useQuery({ queryKey: PLANS_QUERY_KEY, queryFn: fetchPlans })
// 只为反推「哪些套餐还被租户引用」而读：账号行里带着它所属租户的当前套餐。
const accountsQuery = useQuery({ queryKey: ACCOUNTS_QUERY_KEY, queryFn: fetchAccounts })

const plans = computed(() => plansQuery.data.value?.plans ?? [])
const loading = computed(() => plansQuery.isPending.value)
const loadError = computed(() =>
  plansQuery.isError.value ? errorText(plansQuery.error.value) : '',
)

/** 已载入的账号里仍然指向的套餐 id。 */
const referencedPlanIds = computed(() => {
  const ids = new Set<number>()
  for (const account of accountsQuery.data.value?.accounts ?? []) {
    if (account.tenant_id !== null && account.plan !== null) {
      ids.add(account.plan.plan_id)
    }
  }
  return ids
})

/** 引用信息是否可信：账号列表读成功之前，不显示任何删除入口。 */
const referenceKnown = computed(() => accountsQuery.isSuccess.value)

function isReferenced(plan: PlanData): boolean {
  return referencedPlanIds.value.has(plan.plan_id)
}

function canDelete(plan: PlanData): boolean {
  return referenceKnown.value && !isReferenced(plan)
}

function editableOf(plan: PlanData): PlanEditableFields {
  return {
    name: plan.name,
    allowed_profiles: pickKnownProfiles(plan.allowed_profiles),
    hybrid_allowed: plan.hybrid_allowed,
    rerank_allowed: plan.rerank_allowed,
    query_rewrite_allowed: plan.query_rewrite_allowed,
    research_allowed: plan.research_allowed,
    retrieve_qps: plan.retrieve_qps,
    retrieve_daily: plan.retrieve_daily,
    max_kb: plan.max_kb,
    max_kb_per_retrieve: plan.max_kb_per_retrieve,
    max_documents_per_kb: plan.max_documents_per_kb,
    max_processing_documents: plan.max_processing_documents,
  }
}

function emptyDraft(): PlanEditableFields {
  return {
    name: '',
    allowed_profiles: [],
    hybrid_allowed: false,
    rerank_allowed: false,
    query_rewrite_allowed: false,
    research_allowed: false,
    retrieve_qps: 1,
    retrieve_daily: 1,
    max_kb: 1,
    max_kb_per_retrieve: 1,
    max_documents_per_kb: 1,
    max_processing_documents: 1,
  }
}

// 表单草稿用 reactive 对象而不是 `ref<PlanEditableFields | null>`：对话框可以关，
// 但输入框的 v-model 永远有一个合法对象可绑，模板里不用到处判空。
const draft = reactive<PlanEditableFields>(emptyDraft())
const editingPlan = ref<PlanData | null>(null)
const dialogVisible = ref(false)
const deletingPlanId = ref<number | null>(null)

/**
 * 两步保存走通用组合式函数：试算 → （有影响才）确认 → 真写。
 *
 * 判定与文案仍留在本页：套餐的 `PlanImpact` 形状（租户超库数 / 失去档位）与检索参数页
 * 的 `SettingsImpact` 不同，确认条件也只有本页能判。组合式函数只负责顺序与 loading。
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

function unitOf(key: PlanNumericField): string {
  return PLAN_NUMBER_FIELDS.find((field) => field.key === key)?.unit ?? ''
}

function capabilityText(plan: PlanData): string {
  const enabled = PLAN_CAPABILITIES.filter((capability) => plan[capability.key]).map(
    (capability) => capability.label,
  )
  return enabled.length === 0 ? '—' : enabled.join('、')
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
  if (draft.allowed_profiles.length === 0) {
    // 后端也会拒（20018），这里先说清楚原因：空白名单等于把这个套餐下的 Key 当场停用。
    ElMessage.warning('至少勾选一个档位：空白名单会让这个套餐下的 Key 当场全部不可用')
    return
  }
  for (const field of PLAN_NUMBER_FIELDS) {
    const value = draft[field.key]
    if (!Number.isInteger(value) || value < 1) {
      ElMessage.warning(`${field.label}必须是 ≥ 1 的整数（单位：${field.unit}）`)
      return
    }
  }
  if (!draft.allowed_profiles.every((code) => (KEY_PROFILES as readonly string[]).includes(code))) {
    ElMessage.warning('档位只能从 speed / balanced / quality / custom 里选')
    return
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

async function remove(plan: PlanData): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除套餐「${plan.name}」（${plan.code}）？删除不可撤销。` +
        '若仍有租户引用它，后端会拒绝并提示。',
      '删除套餐',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  deletingPlanId.value = plan.plan_id
  try {
    await deletePlan(plan.plan_id)
    ElMessage.success('套餐已删除')
    await queryClient.invalidateQueries({ queryKey: PLANS_QUERY_KEY })
  } catch (error) {
    // 20019（仍被引用）在这里兜底：账号列表推不出「没有账号的租户」，
    // 所以「没显示删除按钮」不是后端判定的替代品，这条错误提示才是最后一道说明。
    ElMessage.error(errorText(error))
  } finally {
    deletingPlanId.value = null
  }
}

async function refresh(): Promise<void> {
  await Promise.all([plansQuery.refetch(), accountsQuery.refetch()])
}
</script>

<template>
  <section class="rounded-lg border border-line bg-white p-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold text-slate-800">套餐管理</h2>
        <p class="mt-1 text-sm text-slate-500">
          保存分两步：先试算影响面（多少租户会超库数、多少密钥配置会失去档位），确认后才落库。
          改动即时生效，不需要重启。
        </p>
        <p class="mt-1 text-xs text-slate-400">
          删除入口依据「账号列表里仍指向它的租户」推断；没有账号的租户推不出来，删除时由后端 20019 兜底。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="plans.length > 0" class="text-sm text-slate-500">共 {{ plans.length }} 档</span>
        <el-button size="small" :loading="plansQuery.isFetching.value" @click="refresh">
          刷新
        </el-button>
      </div>
    </div>

    <div v-if="loading" class="py-10 text-center text-sm text-slate-500">加载套餐…</div>

    <div
      v-else-if="loadError !== ''"
      class="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      <p>套餐列表读取失败：{{ loadError }}</p>
      <el-button class="mt-2" size="small" @click="refresh">重试</el-button>
    </div>

    <div v-else class="mt-4 overflow-x-auto">
      <table class="min-w-full text-sm">
        <thead class="bg-[#F8F9FA] text-[#6B7280]">
          <tr>
            <th class="px-3 py-2 text-left font-medium">套餐</th>
            <th class="px-3 py-2 text-left font-medium">档位白名单</th>
            <th class="px-3 py-2 text-left font-medium">能力开关</th>
            <th class="px-3 py-2 text-left font-medium">上限</th>
            <th class="px-3 py-2 text-left font-medium">更新时间</th>
            <th class="px-3 py-2 text-left font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="plan in plans" :key="plan.plan_id" class="border-t border-line align-top">
            <td class="px-3 py-3">
              <p class="font-medium text-slate-800">{{ plan.name }}</p>
              <p class="font-mono text-xs text-slate-500">{{ plan.code }}</p>
            </td>
            <td class="px-3 py-3 text-slate-600">
              {{ plan.allowed_profiles.length === 0 ? '—' : plan.allowed_profiles.join(' / ') }}
            </td>
            <td class="px-3 py-3 text-slate-600">{{ capabilityText(plan) }}</td>
            <td class="px-3 py-3">
              <!-- 每个数值都带单位：库里只有数字，单位只由列名暗示，不写出来就要人回去查。 -->
              <div
                v-for="field in PLAN_NUMBER_FIELDS"
                :key="field.key"
                class="whitespace-nowrap text-slate-600"
              >
                {{ field.label }}：{{ plan[field.key] }} {{ field.unit }}
              </div>
            </td>
            <td class="px-3 py-3 whitespace-nowrap text-slate-600">
              {{ formatDateTime(plan.updated_at) }}
            </td>
            <td class="px-3 py-3">
              <div class="flex flex-wrap items-center gap-2">
                <el-button size="small" @click="openEdit(plan)">编辑</el-button>
                <el-button
                  v-if="canDelete(plan)"
                  size="small"
                  type="danger"
                  plain
                  :loading="deletingPlanId === plan.plan_id"
                  @click="remove(plan)"
                >
                  删除
                </el-button>
                <span v-else class="text-xs text-slate-400">
                  {{ referenceKnown ? '有租户引用，不显示删除' : '引用信息未载入' }}
                </span>
              </div>
            </td>
          </tr>

          <tr v-if="plans.length === 0">
            <td colspan="6" class="px-3 py-8 text-center text-sm text-slate-500">
              还没有套餐。套餐行由 `scripts/seed_plans.py` 首次播种。
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      title="编辑套餐"
      width="640px"
      :close-on-click-modal="false"
    >
      <div class="space-y-4 text-sm">
        <div class="flex flex-wrap items-center gap-2">
          <span class="w-24 text-slate-500">套餐标识</span>
          <span class="font-mono text-slate-700">{{ editingPlan?.code ?? '—' }}</span>
          <span class="text-xs text-slate-400">
            （code 是脚本与日志的指代，本页不提供改名）
          </span>
        </div>

        <label class="flex items-center gap-2">
          <span class="w-24 text-slate-500">套餐名</span>
          <el-input v-model="draft.name" maxlength="128" />
        </label>

        <div>
          <p class="mb-1 text-slate-500">档位白名单（四个勾选框，没有自由输入）</p>
          <el-checkbox-group v-model="draft.allowed_profiles">
            <el-checkbox-button v-for="code in KEY_PROFILES" :key="code" :value="code">
              {{ code }}
            </el-checkbox-button>
          </el-checkbox-group>
        </div>

        <div>
          <p class="mb-1 text-slate-500">能力开关</p>
          <div class="flex flex-wrap gap-x-6 gap-y-2">
            <label
              v-for="capability in PLAN_CAPABILITIES"
              :key="capability.key"
              class="flex items-center gap-2"
            >
              <el-switch v-model="draft[capability.key]" />
              <span class="text-slate-600">{{ capability.label }}</span>
            </label>
          </div>
        </div>

        <div>
          <p class="mb-1 text-slate-500">上限（每项都必须 ≥ 1）</p>
          <div class="grid grid-cols-2 gap-3">
            <label
              v-for="field in PLAN_NUMBER_FIELDS"
              :key="field.key"
              class="flex items-center gap-2"
            >
              <span class="w-32 shrink-0 text-slate-600">{{ field.label }}</span>
              <el-input-number
                v-model="draft[field.key]"
                :min="1"
                :step="1"
                controls-position="right"
                class="w-32"
              />
              <span class="text-xs text-slate-400">{{ unitOf(field.key) }}</span>
            </label>
          </div>
        </div>

        <p class="rounded bg-[#F8F9FA] px-3 py-2 text-xs text-slate-500">
          点「保存」只先试算：有租户会超库数或失去档位时，会再弹一次确认，确认后才落库。
        </p>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>
