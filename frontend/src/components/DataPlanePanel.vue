<script setup lang="ts">
/**
 * 「这把 Key 能调哪些接口」+ 只读试跑。
 *
 * 为什么单独一块而不是塞进检索页主体：这是一张**清单**（9 条路由、门禁、能不能跑），
 * 与「发一次检索」是两件事；混在一屏里，日常检索会被这些噪声挤下去。
 *
 * 三条硬规矩（都由 `utils/dataPlaneEndpoints` 与它的测试钉住，这里只负责画）：
 * 1. 写操作只列出、**不提供试跑**——真实 Key + 真实租户数据，一个误点就真改了东西；
 * 2. 需要库的那条（`/documents`）拿「当前勾选的第一个库」去试，没勾库就不给按钮；
 * 3. 结果是**原样 JSON**：这是诊断，不是业务渲染。建模成类型反而会掩盖「真实响应长什么样」。
 */
import { computed, ref } from 'vue'

import { ApiError } from '../lib/envelope'
import {
  fetchDataPlaneAuthMe,
  fetchDocumentList,
  fetchMyKnowledgeBases,
} from '../services/scopeService'
import {
  DATA_PLANE_ENDPOINTS,
  canTrial,
  type DataPlaneEndpoint,
  type TrialKind,
} from '../utils/dataPlaneEndpoints'
import { errorText } from '../utils/errorText'

const props = defineProps<{
  /** 当前勾选的库里第一个；`/documents` 的试跑要用它。 */
  kbId: string | null
  /** 有没有选 Key：没选就整块提示去选，而不是给一堆必然失败的按钮。 */
  hasKey: boolean
}>()

interface TrialState {
  loading: boolean
  error: string | null
  data: unknown
}

const trials = ref<Record<string, TrialState>>({})

function stateOf(key: string): TrialState {
  return trials.value[key] ?? { loading: false, error: null, data: null }
}

function endpointKey(endpoint: DataPlaneEndpoint): string {
  return `${endpoint.method} ${endpoint.path}`
}

async function runTrial(endpoint: DataPlaneEndpoint): Promise<void> {
  const kind = endpoint.trial
  if (kind === undefined) return
  const key = endpointKey(endpoint)
  trials.value = { ...trials.value, [key]: { loading: true, error: null, data: null } }
  try {
    const data = await callEndpoint(kind, props.kbId)
    trials.value = { ...trials.value, [key]: { loading: false, error: null, data } }
  } catch (error) {
    trials.value = {
      ...trials.value,
      [key]: { loading: false, error: errorText(error), data: null },
    }
  }
}

function callEndpoint(kind: TrialKind, kbId: string | null): Promise<unknown> {
  if (kind === 'knowledge-bases') return fetchMyKnowledgeBases()
  if (kind === 'auth-me') return fetchDataPlaneAuthMe()
  // canTrial 已经保证只有 kbId 非空时按钮才出现；这里再挡一次是为了让类型收敛，
  // 也避免将来有人绕过 canTrial 直接调它。
  if (kbId === null || kbId === '') {
    return Promise.reject(new ApiError(-1, 'data', '', '这条试跑需要一个知识库 id'))
  }
  return fetchDocumentList(kbId)
}

/** 原始响应渲染：截断长数组，避免一屏被几十条文档撑爆。 */
function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const rows = computed(() => DATA_PLANE_ENDPOINTS)
</script>

<template>
  <div class="space-y-3">
    <p class="text-xs leading-relaxed text-muted">
      数据面（`Authorization: Bearer` 这把 Key 的明文）。每条都注明受什么限制；
      <strong class="text-ink">写入类端点只列出、不代跑</strong>——真实 Key 打的是真实数据，
      误点就真的改了东西。写操作请去知识库管理页做，那里有确认与回执。
    </p>

    <p v-if="!hasKey" class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-xs text-[#6E4E12]">
      先选一把 Key —— 这些接口的身份就是那把 Key。
    </p>

    <div class="overflow-hidden rounded-control border border-line">
      <table class="w-full border-collapse text-xs">
        <thead class="bg-surface-muted">
          <tr>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">方法</th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">路径</th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">用途 / 限制</th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="endpoint in rows" :key="endpointKey(endpoint)" class="align-top">
            <td class="border-b border-line px-2 py-1.5 font-mono text-xs text-slate-500">
              {{ endpoint.method }}
            </td>
            <td class="border-b border-line px-2 py-1.5 font-mono text-xs text-ink">
              {{ endpoint.path }}
            </td>
            <td class="border-b border-line px-2 py-1.5">
              <div class="text-slate-700">{{ endpoint.purpose }}</div>
              <div class="mt-0.5 text-xs text-muted">限制：{{ endpoint.gate }}</div>
            </td>
            <td class="border-b border-line px-2 py-1.5">
              <span v-if="endpoint.write === true" class="text-xs text-muted">写：不代跑</span>
              <template v-else-if="endpoint.trial !== undefined">
                <el-button
                  v-if="canTrial(endpoint, { kbId: props.kbId }) && hasKey"
                  link
                  size="small"
                  :loading="stateOf(endpointKey(endpoint)).loading"
                  @click="runTrial(endpoint)"
                >
                  试一下
                </el-button>
                <span v-else class="text-xs text-muted">
                  {{ endpoint.trialNeeds ?? '先选 Key' }}
                </span>
              </template>
              <span v-else class="text-xs text-muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 试跑结果：原样 JSON。这是诊断，不是业务渲染。 -->
    <div
      v-for="endpoint in rows.filter((item) => item.trial !== undefined)"
      :key="`result-${endpointKey(endpoint)}`"
    >
      <template v-if="stateOf(endpointKey(endpoint)).data !== null || stateOf(endpointKey(endpoint)).error !== null">
        <p class="mt-1 font-mono text-xs text-slate-500">
          {{ endpoint.method }} {{ endpoint.path }} 返回
        </p>
        <p
          v-if="stateOf(endpointKey(endpoint)).error !== null"
          class="mt-1 rounded-control border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-700"
        >
          {{ stateOf(endpointKey(endpoint)).error }}
        </p>
        <pre
          v-else
          class="mt-1 max-h-52 overflow-auto rounded-control bg-slate-50 p-2 text-xs leading-relaxed text-slate-700"
        >{{ prettyJson(stateOf(endpointKey(endpoint)).data) }}</pre>
      </template>
    </div>
  </div>
</template>
