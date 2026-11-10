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

import {
  ACCOUNTS_QUERY_KEY,
  PLANS_QUERY_KEY,
  fetchAccounts,
  fetchPlans,
  updateAccount,
} from '../../services/consoleAdmin'
import type { AccountItem, AccountPatchRequest } from '../../types/consoleAdmin'
import { errorText } from '../../utils/errorText'

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

/** 账号/租户状态的对外文案。未知状态原样显示：不猜（猜错会让人以为账号「正常」）。 */
const STATUS_LABELS: Record<string, string> = { active: '正常', disabled: '已停用' }

function statusLabel(status: string | null): string {
  if (status === null || status === '') return '—'
  return STATUS_LABELS[status] ?? status
}

/**
 * 颜色只取事实底座 §10 与前端约定 §状态与颜色 的语义色：正常用「成功」绿，
 * 已停用用「失败」红。账号状态不是文档三态，所以不走 `StatusBadge.vue`（那张表
 * 的 1/2/3 是文档处理状态），但配色的出处仍是同一组令牌。
 *
 * 文字色从原来的 #065F46 / #B91C1C 换成暖色系的同义色：冷绿冷红压在米白底上会
 * 显出「系统自带控件」的味道，也与 StatusBadge 的暖色徽章对不上。对比度都 ≥4.5:1。
 */
function statusClass(status: string | null): string {
  if (status === 'active') return 'text-[#3C5228]'
  if (status === 'disabled') return 'text-[#8C2F22]'
  return 'text-slate-500'
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
  <section class="card card-body">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="page-title">账号管理</h2>
        <p class="page-subtitle">
          跨租户查看全部账号。停用账号 / 停用租户 / 换套餐都改服务端判定，下一次请求即生效，不需要重启。
        </p>
      </div>
      <div class="flex items-center gap-2 text-sm text-slate-500">
        <span v-if="accounts.length > 0">共 {{ accounts.length }} 个账号</span>
        <el-button size="small" :loading="accountsQuery.isFetching.value" @click="refresh">
          刷新
        </el-button>
      </div>
    </div>

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

    <div v-else class="mt-4 overflow-x-auto">
      <table class="min-w-full text-sm">
        <thead class="bg-[#F8F9FA] text-[#6B7280]">
          <tr>
            <th class="px-3 py-2 text-left font-medium">用户名</th>
            <th class="px-3 py-2 text-left font-medium">角色</th>
            <th class="px-3 py-2 text-left font-medium">账号状态</th>
            <th class="px-3 py-2 text-left font-medium">所属租户</th>
            <th class="px-3 py-2 text-left font-medium">租户状态</th>
            <th class="px-3 py-2 text-left font-medium">当前套餐</th>
            <th class="px-3 py-2 text-left font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in accounts"
            :key="row.account_id"
            class="border-t border-line align-top"
          >
            <td class="px-3 py-3 font-medium text-slate-800">{{ row.username }}</td>
            <td class="px-3 py-3 text-slate-600">{{ roleLabel(row.role) }}</td>
            <td class="px-3 py-3">
              <span :class="statusClass(row.status)">{{ statusLabel(row.status) }}</span>
            </td>
            <td class="px-3 py-3 text-slate-600">{{ tenantLabel(row) }}</td>
            <td class="px-3 py-3">
              <span :class="statusClass(row.tenant_status)">
                {{ statusLabel(row.tenant_status) }}
              </span>
            </td>
            <td class="px-3 py-3 text-slate-600">{{ planLabel(row) }}</td>
            <td class="px-3 py-3">
              <div class="flex flex-wrap items-center gap-2">
                <el-button size="small" :disabled="saving" @click="toggleAccount(row)">
                  {{ row.status === 'active' ? '停用账号' : '启用账号' }}
                </el-button>

                <!-- 管理员不属于任何租户：租户那两项对他是 20002，所以不渲染入口，不让点了才报错。 -->
                <template v-if="row.tenant_id !== null">
                  <el-button size="small" :disabled="saving" @click="toggleTenant(row)">
                    {{ row.tenant_status === 'active' ? '停用租户' : '启用租户' }}
                  </el-button>
                  <el-select
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
                </template>
              </div>
            </td>
          </tr>

          <tr v-if="accounts.length === 0">
            <td colspan="7" class="px-3 py-8 text-center text-sm text-slate-500">
              还没有账号。管理员账号由 `scripts/create_admin.py` 创建，租户账号由后续工具创建。
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
