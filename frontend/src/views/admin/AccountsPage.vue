<script setup lang="ts">
/**
 * 账号管理页：管理员跨租户看账号，并对三件事做写操作 —— 停用/启用账号、
 * 停用/启用所属租户、换租户的套餐。
 *
 * 三条纪律写在页面里，因为它们是「页面什么时候请求」而不是服务层的事：
 *
 * 1. **每个写操作都先二次确认**：这三项都立刻影响线上判定，误点一次就是客户调不动；
 * 2. **成功后失效账号查询**：服务端改完即生效（判定每次现读账号/租户/套餐），页面只负责
 *    把缓存丢掉、下一次请求拿到新值，不做本地「乐观改一行」——乐观更新会让「后端到底改没改」
 *    变成一个看不见的问题；
 * 3. **停用只改状态、不删会话行**（后端行为）：旧 cookie 在下一次请求就被状态判定拦下，
 *    所以文案写「下一次请求失效」，不写「立即退出登录」。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed } from 'vue'

import PageHeader from '../../components/PageHeader.vue'
import {
  ACCOUNTS_QUERY_KEY,
  PLANS_QUERY_KEY,
  fetchAccounts,
  fetchPlans,
  updateAccount,
} from '../../services/consoleAdmin'
import type { AccountItem, AccountPatchRequest } from '../../types/consoleAdmin'
import { errorText } from '../../utils/errorText'
import { statusLabel as sharedStatusLabel, statusPillStyle } from '../../utils/statusPill'

const queryClient = useQueryClient()

const accountsQuery = useQuery({ queryKey: ACCOUNTS_QUERY_KEY, queryFn: fetchAccounts })
// 换套餐下拉的数据源。与套餐页共用 query key：那边改完套餐，这边下一次读取拿到新列表。
const plansQuery = useQuery({ queryKey: PLANS_QUERY_KEY, queryFn: fetchPlans })

const accounts = computed(() => accountsQuery.data.value?.accounts ?? [])
const plans = computed(() => plansQuery.data.value?.plans ?? [])
const loading = computed(() => accountsQuery.isPending.value)
const loadError = computed(() =>
  accountsQuery.isError.value ? errorText(accountsQuery.error.value) : '',
)
const plansError = computed(() =>
  plansQuery.isError.value ? errorText(plansQuery.error.value) : '',
)

const saveMutation = useMutation({
  mutationFn: (input: { accountId: string; patch: AccountPatchRequest }) =>
    updateAccount(input.accountId, input.patch),
  onSuccess: async () => {
    ElMessage.success('已保存：下一次请求即按新设置判定')
    // 账号行里的套餐 / 两个状态列都变了，整份列表重读一次最省心（行数不多）。
    await queryClient.invalidateQueries({ queryKey: ACCOUNTS_QUERY_KEY })
  },
  onError: (error) => {
    ElMessage.error(errorText(error))
  },
})

const saving = computed(() => saveMutation.isPending.value)

/**
 * 二次确认。`ElMessageBox` 在用户点「取消」时以 reject 结束 —— 那是用户的决定，
 * 不是请求失败，所以这里把它当「什么都不做」返回，不弹错误消息。
 */
async function confirmThen(message: string, run: () => void): Promise<void> {
  try {
    await ElMessageBox.confirm(message, '确认操作', {
      type: 'warning',
      confirmButtonText: '确认',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  run()
}

/**
 * 状态文案与配色都来自 `utils/statusPill.ts`（与 Key 列表同一份）。
 *
 * 之前这里有一份 `STATUS_LABELS` + `statusClass`，KeysPage 又自己拼了一个胶囊——
 * 同一件事两处实现，改配色必然漏一处。现在只剩本地这一层：把「空值」显示成「—」。
 */
function statusLabel(status: string | null): string {
  if (status === null || status === '') return '—'
  return sharedStatusLabel(status)
}

/** 空状态不摆胶囊：一个空的彩色块会被读成「某种状态」。 */
function hasStatus(status: string | null): boolean {
  return status !== null && status !== ''
}

function roleLabel(role: string): string {
  if (role === 'admin') return '管理员'
  if (role === 'user') return '租户账号'
  return role
}

function tenantLabel(row: AccountItem): string {
  if (row.tenant_id === null) return '不属于任何租户'
  return row.tenant_name ?? row.tenant_id
}

function planLabel(row: AccountItem): string {
  return row.plan === null ? '—' : `${row.plan.name}（${row.plan.code}）`
}

async function toggleAccount(row: AccountItem): Promise<void> {
  const target = row.status === 'active' ? 'disabled' : 'active'
  await confirmThen(
    target === 'disabled'
      ? `确认停用账号「${row.username}」？停用后它已登录的会话会在下一次请求失效。`
      : `确认启用账号「${row.username}」？`,
    () => saveMutation.mutate({ accountId: row.account_id, patch: { status: target } }),
  )
}

async function toggleTenant(row: AccountItem): Promise<void> {
  if (row.tenant_id === null) return
  const target = row.tenant_status === 'active' ? 'disabled' : 'active'
  await confirmThen(
    target === 'disabled'
      ? `确认停用租户「${tenantLabel(row)}」？这个租户下所有账号与 Key 的下一次调用都会被拦下。`
      : `确认启用租户「${tenantLabel(row)}」？`,
    () => saveMutation.mutate({
      accountId: row.account_id,
      patch: { tenant_status: target },
    }),
  )
}

/**
 * 换套餐：`plan_id` 改的是**账号所属租户**的套餐（后端契约），不是账号自己的属性。
 * 下拉的 `model-value` 绑在服务端返回的当前套餐上：用户取消确认时列表不会提前变样，
 * 成功则由失效查询把新值读回来（不在前端先改一行）。
 */
function onPlanChange(row: AccountItem, value: unknown): void {
  if (typeof value !== 'number') return
  const plan = plans.value.find((item) => item.plan_id === value)
  void confirmThen(
    `确认把租户「${tenantLabel(row)}」的套餐换成「${plan?.name ?? value}」？` +
      '套餐决定租户的额度上限，换完下一次请求即按新套餐判定。',
    () => saveMutation.mutate({ accountId: row.account_id, patch: { plan_id: value } }),
  )
}

async function refresh(): Promise<void> {
  await Promise.all([accountsQuery.refetch(), plansQuery.refetch()])
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader title="账号管理" subtitle="跨租户查看与调整账号，改动下一次请求即生效。">
      <template #actions>
        <span v-if="accounts.length > 0" class="text-sm text-muted">
          {{ accounts.length }} 个账号
        </span>
        <el-button size="small" :loading="accountsQuery.isFetching.value" @click="refresh">
          刷新
        </el-button>
      </template>
    </PageHeader>

    <section class="card card-body">
    <p
      v-if="plansError !== ''"
      class="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      套餐列表读取失败，换套餐下拉暂时不可用：{{ plansError }}
    </p>

    <div v-if="loading" class="py-10 text-center text-sm text-slate-500">加载账号…</div>

    <div
      v-else-if="loadError !== ''"
      class="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      <p>账号列表读取失败：{{ loadError }}</p>
      <el-button class="mt-2" size="small" @click="refresh">重试</el-button>
    </div>

    <div v-else class="mt-5 overflow-x-auto">
      <div v-if="accounts.length === 0" class="py-10 text-center">
        <p class="text-[15px] font-semibold text-ink">还没有账号</p>
        <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted">
          管理员账号由 <span class="font-mono">scripts/create_admin.py</span> 创建，租户账号由后续工具创建。
        </p>
      </div>

      <table v-else class="min-w-full text-sm">
        <thead>
          <tr>
            <th class="th-label px-3 py-2 text-left">用户名</th>
            <th class="th-label px-3 py-2 text-left">角色</th>
            <th class="th-label px-3 py-2 text-left">账号状态</th>
            <th class="th-label px-3 py-2 text-left">所属租户</th>
            <th class="th-label px-3 py-2 text-left">租户状态</th>
            <th class="th-label px-3 py-2 text-left">当前套餐</th>
            <th class="th-label px-3 py-2 text-left">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in accounts"
            :key="row.account_id"
            class="border-t border-line align-top transition-colors hover:bg-surface-muted/60"
          >
            <td class="px-3 py-3 font-medium text-ink">{{ row.username }}</td>
            <td class="px-3 py-3 text-slate-600">{{ roleLabel(row.role) }}</td>
            <td class="px-3 py-3">
              <span
                v-if="hasStatus(row.status)"
                class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
                :style="statusPillStyle(row.status)"
                >{{ statusLabel(row.status) }}</span
              >
              <span v-else class="text-slate-400">—</span>
            </td>
            <td class="px-3 py-3 text-slate-600">{{ tenantLabel(row) }}</td>
            <td class="px-3 py-3">
              <span
                v-if="hasStatus(row.tenant_status)"
                class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
                :style="statusPillStyle(row.tenant_status)"
                >{{ statusLabel(row.tenant_status) }}</span
              >
              <span v-else class="text-slate-400">—</span>
            </td>
            <td class="px-3 py-3 text-slate-600">{{ planLabel(row) }}</td>
            <!--
              三个入口（停用账号 / 停用租户 / 换套餐）并排放在一行时会挤出表格：
              下拉本身就有 176px，加上两个按钮再折行，操作列被撑得很宽。改成两行：
              第一行两个「停用 / 启用」，第二行换套餐下拉。
            -->
            <td class="px-3 py-3">
              <div class="flex flex-col gap-2">
                <div class="flex flex-wrap items-center gap-2">
                  <el-button size="small" :disabled="saving" @click="toggleAccount(row)">
                    {{ row.status === 'active' ? '停用账号' : '启用账号' }}
                  </el-button>

                  <!-- 管理员不属于任何租户：租户那两项对他是 20002，所以不渲染入口，不让点了才报错。 -->
                  <el-button
                    v-if="row.tenant_id !== null"
                    size="small"
                    :disabled="saving"
                    @click="toggleTenant(row)"
                  >
                    {{ row.tenant_status === 'active' ? '停用租户' : '启用租户' }}
                  </el-button>
                </div>
                <el-select
                  v-if="row.tenant_id !== null"
                  :model-value="row.plan?.plan_id"
                  placeholder="换套餐"
                  size="small"
                  class="w-44"
                  :disabled="saving || plans.length === 0"
                  @change="(value) => onPlanChange(row, value)"
                >
                  <el-option
                    v-for="plan in plans"
                    :key="plan.plan_id"
                    :value="plan.plan_id"
                    :label="`${plan.name}（${plan.code}）`"
                  />
                </el-select>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    </section>
  </section>
</template>
