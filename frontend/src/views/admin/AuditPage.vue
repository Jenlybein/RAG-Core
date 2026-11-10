<script setup lang="ts">
/**
 * 审计日志页：只读，不提供任何写入口。
 *
 * **本期后端只支持 `limit`**（缺省 50、上限 200），没有 offset、也没有「按动作过滤」的查询
 * 参数。因此这里的分页与动作过滤都发生在**已经载入的这批记录**上：
 *
 * - 「条数」下拉决定 `GET /console/audit-logs?limit=N` 载入最近多少条；
 * - 动作过滤、表格翻页只筛/切这批记录，不伪造后端不存在的 offset 协议。
 *
 * 这个取舍写清楚是为了不让人误以为「翻到第 3 页就能看到更早的记录」——想更早的记录，
 * 只能调大条数重新载入。真正的服务端分页是后续票的事。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'

import { auditLogsQueryKey, fetchAuditLogs } from '../../services/consoleAdmin'
import { formatDateTime } from '../../utils/datetime'
import { errorText } from '../../utils/errorText'

const limit = ref(50)

const auditQuery = useQuery({
  queryKey: computed(() => auditLogsQueryKey(limit.value)),
  queryFn: () => fetchAuditLogs(limit.value),
})

const logs = computed(() => auditQuery.data.value?.audit_logs ?? [])
const loading = computed(() => auditQuery.isPending.value)
const loadError = computed(() =>
  auditQuery.isError.value ? errorText(auditQuery.error.value) : '',
)

/** 动作下拉的选项从已载入的记录里取：没有独立的「动作字典」接口，不自己编一份。 */
const actionOptions = computed(() => {
  const actions = new Set<string>()
  for (const log of logs.value) actions.add(log.action)
  return [...actions].sort()
})

const actionFilter = ref('')
const filteredLogs = computed(() =>
  actionFilter.value === ''
    ? logs.value
    : logs.value.filter((log) => log.action === actionFilter.value),
)

/** 表格内翻页：只在这批已载入的记录里切，见文件头的取舍说明。 */
const PAGE_SIZE = 20
const page = ref(1)
const pagedLogs = computed(() =>
  filteredLogs.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)

// 换条数或换过滤条件后回到第 1 页：否则会停在旧页码上，看到一片空白。
watch([actionFilter, limit, logs], () => {
  page.value = 1
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
          后端本期只支持
          <code class="rounded bg-slate-100 px-1 py-0.5">limit</code>
          （缺省 50、上限 200）：条数决定载入最近多少条，动作过滤与表格翻页只作用于这批记录；
          没有 offset 接口，翻页不会载入更早的记录。
        </p>
      </div>
      <el-button size="small" :loading="auditQuery.isFetching.value" @click="refresh">
        刷新
      </el-button>
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-3 text-sm">
      <label class="flex items-center gap-2 text-slate-500">
        <span>每次载入</span>
        <el-select v-model="limit" size="small" class="w-28">
          <el-option v-for="n in [50, 100, 200]" :key="n" :value="n" :label="`${n} 条`" />
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

      <span class="text-slate-500">
        {{ filteredLogs.length }} / {{ logs.length }} 条
      </span>
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
            <tr v-for="log in pagedLogs" :key="log.audit_id" class="border-t border-line align-top">
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

            <tr v-if="pagedLogs.length === 0">
              <td colspan="4" class="px-3 py-8 text-center text-sm text-slate-500">
                {{ logs.length === 0 ? '还没有审计记录。' : '没有匹配这个动作的记录。' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="filteredLogs.length > PAGE_SIZE" class="mt-4 flex justify-end">
        <el-pagination
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="filteredLogs.length"
          layout="total, prev, pager, next"
          background
          small
        />
      </div>
    </template>
  </section>
</template>
