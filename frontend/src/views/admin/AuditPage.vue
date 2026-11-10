<script setup lang="ts">
/**
 * 审计日志页：只读，不提供任何写入口。
 *
 * 分页与动作过滤都在**服务端**做（`limit` / `offset` / `action`）：审计表只会越来越长，
 * 先拉全量再在浏览器里切片看起来能用，但第一次「翻不到更早的记录」就露馅了。
 * 页面只维护 `page` / `pageSize` / `action` 三个状态，把 `offset = (page-1)*pageSize` 发出去，
 * 用响应里的 `total` 画页码——`total` 是当前筛选条件下的全量条数，所以不会出现空尾页。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'

import { auditLogsQueryKey, fetchAuditLogs } from '../../services/consoleAdmin'
import { formatDateTime } from '../../utils/datetime'
import { errorText } from '../../utils/errorText'

const pageSize = ref(20)
const page = ref(1)
const actionFilter = ref('')

const offset = computed(() => (page.value - 1) * pageSize.value)

const auditQuery = useQuery({
  queryKey: computed(() => auditLogsQueryKey(pageSize.value, offset.value, actionFilter.value)),
  queryFn: () => fetchAuditLogs(pageSize.value, offset.value, actionFilter.value),
})

const rows = computed(() => auditQuery.data.value?.audit_logs ?? [])
const total = computed(() => auditQuery.data.value?.total ?? 0)
const loading = computed(() => auditQuery.isPending.value)
const loadError = computed(() =>
  auditQuery.isError.value ? errorText(auditQuery.error.value) : '',
)

// 换页大小或换动作 → 回第 1 页。不重置就会停在一个越界页码上，看见空白还以为没记录。
watch([pageSize, actionFilter], () => {
  page.value = 1
})

/**
 * 动作下拉的选项来自当前页 + 已选中的那个。
 *
 * 后端没有「动作字典」接口，这里不自己编一份；代价是筛选范围只能从见过的记录里挑，
 * 这比编一份会漂移的清单诚实。
 */
const actionOptions = computed(() => {
  const actions = new Set<string>()
  for (const log of rows.value) actions.add(log.action)
  if (actionFilter.value !== '') actions.add(actionFilter.value)
  return [...actions].sort()
})

function actorLabel(actorId: string | null): string {
  // 系统动作（播种、回填）没有操作者，账本如实为空。显示「系统」而不是编一个账号 id。
  return actorId ?? '系统'
}

async function refresh(): Promise<void> {
  await auditQuery.refetch()
}
</script>

<template>
  <section class="rounded-lg border border-line bg-white p-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold text-slate-800">审计日志</h2>
        <p class="mt-1 text-sm text-slate-500">
          控制面每一次改动的账本：谁、什么时候、把哪个对象改成了什么。本页只读。
        </p>
        <p class="mt-1 text-xs text-slate-400">
          分页与动作过滤都由后端完成（
          <code class="rounded bg-slate-100 px-1 py-0.5">limit</code>
          /
          <code class="rounded bg-slate-100 px-1 py-0.5">offset</code>
          /
          <code class="rounded bg-slate-100 px-1 py-0.5">action</code>
          ）；动作是精确匹配，不是前缀。
        </p>
      </div>
      <el-button size="small" :loading="auditQuery.isFetching.value" @click="refresh">
        刷新
      </el-button>
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-3 text-sm">
      <label class="flex items-center gap-2 text-slate-500">
        <span>每页</span>
        <el-select v-model="pageSize" size="small" class="w-28">
          <el-option v-for="n in [20, 50, 100, 200]" :key="n" :value="n" :label="`${n} 条`" />
        </el-select>
      </label>

      <label class="flex items-center gap-2 text-slate-500">
        <span>动作</span>
        <el-select
          v-model="actionFilter"
          size="small"
          clearable
          placeholder="全部动作"
          class="w-52"
        >
          <el-option
            v-for="action in actionOptions"
            :key="action"
            :value="action"
            :label="action"
          />
        </el-select>
      </label>

      <span class="text-slate-500">共 {{ total }} 条</span>
    </div>

    <div v-if="loading" class="py-10 text-center text-sm text-slate-500">加载审计记录…</div>

    <div
      v-else-if="loadError !== ''"
      class="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      <p>审计列表读取失败：{{ loadError }}</p>
      <el-button class="mt-2" size="small" @click="refresh">重试</el-button>
    </div>

    <template v-else>
      <div class="mt-4 overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-[#F8F9FA] text-[#6B7280]">
            <tr>
              <th class="px-3 py-2 text-left font-medium">时间</th>
              <th class="px-3 py-2 text-left font-medium">动作</th>
              <th class="px-3 py-2 text-left font-medium">目标</th>
              <th class="px-3 py-2 text-left font-medium">操作者</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="log in rows" :key="log.audit_id" class="border-t border-line align-top">
              <td class="whitespace-nowrap px-3 py-3 text-slate-600">
                {{ formatDateTime(log.created_at) }}
              </td>
              <td class="px-3 py-3 font-mono text-slate-800">{{ log.action }}</td>
              <td class="break-all px-3 py-3 font-mono text-slate-600">{{ log.target }}</td>
              <td class="break-all px-3 py-3 text-slate-600">
                <span :class="log.actor_id === null ? 'text-slate-400' : ''">
                  {{ actorLabel(log.actor_id) }}
                </span>
              </td>
            </tr>

            <tr v-if="rows.length === 0">
              <td colspan="4" class="px-3 py-8 text-center text-sm text-slate-500">
                {{ actionFilter === '' ? '还没有审计记录。' : '没有匹配这个动作的记录。' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="total > pageSize" class="mt-4 flex justify-end">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          background
          small
        />
      </div>
    </template>
  </section>
</template>
