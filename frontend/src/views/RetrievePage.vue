<script setup lang="ts">
/**
 * 检索页：勾库 → 发检索 → 结果卡片 + 只读运行摘要。
 *
 * 三条不许越过的线（issue #87 / spec #82）：
 * 1. 请求体只有 `kb_ids` + `query`：本页没有任何档位按钮或高级参数输入，
 *    `profile` / `top_k` / `mode` / `rerank` / `rewrite` 一律不进请求体；
 * 2. 运行摘要只读响应 `metadata`，**不看页面输入**——摘要回答的是「后端这次实际
 *    生效了什么」，拿页面配置冒充它就等于把调试台变成许愿台；
 * 3. 只走数据面客户端（Bearer Key）。选哪把 Key、改密钥配置是 #92 的票。
 *
 * 结果卡片只展示契约里有的字段；`latency_ms` / `log_id` / `trace_id` 等字段后端没有
 * （00-facts §5 明确列过），这里也不编造。
 */
import { useMutation, useQuery } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '../lib/envelope'
import { fetchPickableKnowledgeBases, retrieve } from '../services/ragService'
import { useRetrieveStore } from '../stores/retrieve'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'
import {
  buildRunSummary,
  describeEmptyReason,
  formatPercent,
  formatRank,
  formatScore,
} from '../utils/format'

const route = useRoute()
const store = useRetrieveStore()

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    // code=-1 是协议/连通性问题，没有业务码可报；其余把码带上，方便对照错误码表。
    return error.code === -1 ? error.message : `${error.message}（业务码 ${error.code}）`
  }
  return error instanceof Error ? error.message : String(error)
}

// —— ① 检索范围 ——

/**
 * `?kb_id=` 预填。同样的坑上传页已经踩过：同一路由只改 query 不会重新挂载组件，
 * 只读一次会让勾选停在上一个库，所以初次读一次、之后再 watch。
 */
function prefillFromQuery(value: unknown): void {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string') return
  store.prefill(raw)
}
prefillFromQuery(route.query.kb_id)
watch(
  () => route.query.kb_id,
  (value) => prefillFromQuery(value),
)

const {
  data: kbData,
  error: kbErrorRaw,
  isPending: kbLoading,
  isSuccess: kbLoaded,
} = useQuery({
  // 与 #86 的列表查询分开一个 key：它们将来会走不同的接口（本页只用数据面），
  // 共用一个 key 会让两边的缓存互相覆盖。
  queryKey: ['retrieve', 'pickable-knowledge-bases'],
  queryFn: fetchPickableKnowledgeBases,
  retry: false,
})
const kbs = computed(() => kbData.value ?? [])
const kbError = computed(() => (kbErrorRaw.value === null ? '' : errorText(kbErrorRaw.value)))

const visibleKbIds = computed(() => new Set(kbs.value.map((kb) => kb.kb_id)))
/**
 * 已选但不在数据面可见清单里的库（比如 URL 预填的旧库 ID）。
 *
 * 不静默删掉它们：用户可能正想查一个不在这把 Key 白名单里的库；但必须提前说明
 * 检索大概率会失败，并给一个「移除」的动作。
 */
const unpickableSelected = computed(() =>
  store.selectedKbIds.filter((kbId) => !visibleKbIds.value.has(kbId)),
)

function dropUnpickable(): void {
  store.setSelection(store.selectedKbIds.filter((kbId) => visibleKbIds.value.has(kbId)))
}

/** el-checkbox-group 的事件载荷是联合类型；本页的选项值全是字符串，这里只做类型收窄。 */
function onSelectionChange(values: Array<string | number | boolean>): void {
  store.setSelection(values.filter((value): value is string => typeof value === 'string'))
}

// —— ② 问题与检索 ——
const queryText = ref('')

const trimmedQuery = computed(() => queryText.value.trim())
/** 按钮禁用时把原因说出来：只灰掉按钮不说原因，用户会以为自己点错了。 */
const blockReason = computed(() => {
  if (store.selectedCount === 0) return '先在上方勾选至少一个知识库'
  if (trimmedQuery.value === '') return '先输入要检索的问题（全空白不算问题）'
  return ''
})

// —— ③ 结果 / ④ 摘要 ——
const result = ref<RetrieveData | null>(null)
const runError = ref<{ message: string; limitHint: boolean } | null>(null)

const { mutate, isPending: running } = useMutation({
  mutationFn: (payload: RetrieveRequest) => retrieve(payload),
  onMutate: () => {
    runError.value = null
  },
  onSuccess: (data) => {
    result.value = data
  },
  onError: (error) => {
    // 本次失败就清掉上一次的结果：旧卡片配新报错会让人以为「这次也成功了一部分」，
    // 而摘要更必须永远对应最近一次响应，不能停在上一次。
    result.value = null
    runError.value = {
      message: errorText(error),
      limitHint: error instanceof ApiError && error.code === 20013,
    }
  },
})

function submitRetrieve(): void {
  if (blockReason.value !== '' || running.value) return
  // 拷一份再发：请求在途时用户改勾选，不该顺手改掉这次请求的 `kb_ids`。
  mutate({ kb_ids: [...store.selectedKbIds], query: trimmedQuery.value })
}

const chunks = computed(() => result.value?.retrieved_chunks ?? [])
const emptyReason = computed(() => result.value?.metadata.retrieval.empty_reason ?? null)
const emptyCopy = computed(() =>
  result.value !== null && result.value.retrieved_chunks.length === 0
    ? describeEmptyReason(emptyReason.value)
    : null,
)

const summaryRows = computed(() =>
  result.value === null ? [] : buildRunSummary(result.value.metadata),
)
const degradedReason = computed(() => result.value?.metadata.retrieval.degraded_reason ?? null)
const degraded = computed(() => result.value?.metadata.retrieval.degraded ?? false)
const hasTenantPolicy = computed(() => result.value?.metadata.tenant_policy !== undefined)
</script>

<template>
  <section class="space-y-4">
    <!-- ① 检索范围 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">检索范围</h2>
            <p class="mt-1 text-xs text-slate-500">
              列出数据面可见的库（已按这把 Key 的密钥配置白名单收窄）；检索方式、条数、重排与
              查询改写都由密钥配置决定，本页不设这些参数。
            </p>
          </div>
          <span class="shrink-0 text-sm text-slate-600">
            已选 {{ store.selectedCount }} / 共 {{ kbs.length }}
          </span>
        </div>
      </template>

      <p v-if="kbLoading" class="text-sm text-slate-400">正在读取可勾选的知识库…</p>
      <div
        v-else-if="kbError !== ''"
        class="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
      >
        {{ kbError }}
      </div>
      <div
        v-else-if="kbs.length === 0"
        class="rounded border border-line bg-slate-50 px-3 py-2 text-sm text-slate-600"
      >
        数据面看不到任何知识库：先创建知识库并上传文档，再回来检索。
        <RouterLink to="/" class="text-brand underline">去建库 / 上传文档</RouterLink>
      </div>
      <el-checkbox-group
        v-else
        class="grid gap-2 sm:grid-cols-2"
        :model-value="store.selectedKbIds"
        @update:model-value="onSelectionChange"
      >
        <el-checkbox v-for="kb in kbs" :key="kb.kb_id" :value="kb.kb_id" class="!mr-0">
          <span class="text-sm text-slate-700">{{ kb.name }}</span>
          <span class="ml-2 font-mono text-xs text-slate-400">{{ kb.kb_id }}</span>
        </el-checkbox>
      </el-checkbox-group>

      <div
        v-if="kbLoaded && unpickableSelected.length > 0"
        class="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
      >
        <p>这些已选库不在数据面可见清单里（可能不在这把 Key 的白名单里），带它们检索大概率会失败：</p>
        <p class="mt-1 break-all font-mono">{{ unpickableSelected.join('、') }}</p>
        <el-button class="mt-2" size="small" @click="dropUnpickable">移除这些选择</el-button>
      </div>
    </el-card>

    <!-- ② 问题 -->
    <el-card shadow="never">
      <template #header>
        <div>
          <h2 class="text-sm font-semibold text-slate-800">问题</h2>
          <p class="mt-1 text-xs text-slate-500">
            请求体只有 <code class="rounded bg-slate-100 px-1">kb_ids</code> 与
            <code class="rounded bg-slate-100 px-1">query</code>；任何质量参数都不随请求发送。
          </p>
        </div>
      </template>

      <el-form label-position="top" @submit.prevent="submitRetrieve">
        <el-form-item label="要检索的问题（可多行）">
          <el-input
            v-model="queryText"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="例如：退款申请提交后，一般几天能到账？"
          />
        </el-form-item>
        <div class="flex flex-wrap items-center gap-3">
          <el-button
            type="primary"
            native-type="submit"
            :disabled="blockReason !== '' || running"
            :loading="running"
          >
            检索
          </el-button>
          <span v-if="blockReason !== ''" class="text-xs text-slate-500">{{ blockReason }}</span>
          <span v-else class="text-xs text-slate-400">以已选库为范围发一次检索</span>
        </div>
      </el-form>
    </el-card>

    <!-- ③ 召回结果 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">召回结果</h2>
            <p class="mt-1 text-xs text-slate-500">
              分数保留 4 位、语义相似度 1 位；某一格是「—」表示那一路没命中，不是 0 分。
            </p>
          </div>
          <span v-if="result !== null" class="shrink-0 text-sm text-slate-600">
            共 {{ chunks.length }} 条
          </span>
        </div>
      </template>

      <p v-if="running" class="text-sm text-slate-500">正在检索…</p>

      <div
        v-else-if="runError !== null"
        class="rounded border border-red-200 bg-red-50 px-3 py-2"
      >
        <p class="text-sm text-red-700">{{ runError.message }}</p>
        <p v-if="runError.limitHint" class="mt-1 text-xs text-red-600">
          这次要查的库数 / 档位超出限制：少勾几个库再试，或去 Key 管理页调整这把 Key 的密钥配置。
        </p>
      </div>

      <p v-else-if="result === null" class="text-sm text-slate-400">
        还没发起检索：先勾库、写问题，再点「检索」。
      </p>

      <div
        v-else-if="emptyCopy !== null"
        class="rounded border border-line bg-slate-50 px-4 py-3"
      >
        <p class="text-sm font-medium text-slate-800">{{ emptyCopy.title }}</p>
        <p class="mt-1 text-sm text-slate-600">{{ emptyCopy.hint }}</p>
        <p class="mt-2 text-xs text-slate-400">
          metadata.retrieval.empty_reason =
          <span class="font-mono">{{ emptyReason ?? '—' }}</span>
        </p>
      </div>

      <ul v-else class="divide-y divide-line">
        <li v-for="(chunk, index) in chunks" :key="chunk.chunk_id" class="py-4">
          <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span class="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
              {{ formatRank(index + 1) }}
            </span>
            <h3 class="text-sm font-medium text-slate-800">{{ chunk.title || '（无标题）' }}</h3>
            <span v-if="chunk.kb_id !== null" class="text-xs text-slate-400">
              库：<span class="font-mono">{{ chunk.kb_id }}</span>
            </span>
          </div>

          <dl class="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-3 lg:grid-cols-5">
            <div>
              <dt class="text-slate-400">综合排序分</dt>
              <dd class="font-mono text-slate-700">{{ formatScore(chunk.score) }}</dd>
            </div>
            <div>
              <dt class="text-slate-400">语义相似度</dt>
              <dd
                class="font-mono"
                :class="chunk.vector_score === null ? 'text-slate-400' : 'text-slate-700'"
              >
                {{ formatPercent(chunk.vector_score) }}
              </dd>
            </div>
            <div>
              <dt class="text-slate-400">关键词匹配分</dt>
              <dd
                class="font-mono"
                :class="chunk.bm25_score === null ? 'text-slate-400' : 'text-slate-700'"
              >
                {{ formatScore(chunk.bm25_score) }}
              </dd>
            </div>
            <div>
              <dt class="text-slate-400">语义名次</dt>
              <dd class="font-mono text-slate-700">{{ formatRank(chunk.vector_rank) }}</dd>
            </div>
            <div>
              <dt class="text-slate-400">关键词名次</dt>
              <dd class="font-mono text-slate-700">{{ formatRank(chunk.bm25_rank) }}</dd>
            </div>
          </dl>

          <div class="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-500">
            <span>文档 ID：<span class="font-mono">{{ chunk.document_id }}</span></span>
            <span>切片 ID：<span class="font-mono">{{ chunk.chunk_id }}</span></span>
            <span>章节：{{ chunk.heading_path ?? '—' }}</span>
            <span>块类型：{{ chunk.expansion ?? '—' }}</span>
          </div>
        </li>
      </ul>
    </el-card>

    <!-- ④ 运行摘要（只读） -->
    <el-card shadow="never">
      <template #header>
        <div>
          <h2 class="text-sm font-semibold text-slate-800">运行摘要（只读）</h2>
          <p class="mt-1 text-xs text-slate-500">
            每个值都取自这一次响应的 metadata，不读页面上的任何输入；英文码照原文显示，中文只进
            tooltip。
          </p>
        </div>
      </template>

      <p v-if="result === null" class="text-sm text-slate-400">
        还没有响应：跑一次检索后，这里显示后端这次实际生效的值。
      </p>

      <div v-else>
        <div
          v-if="degraded"
          class="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
        >
          本次结果不完整（degraded = true）：{{ degradedReason ?? '后端没有给出原因' }}。
          降级那一路的分数、名次会是「—」。
        </div>

        <dl class="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
          <div v-for="row in summaryRows" :key="row.field">
            <dt class="text-xs text-slate-400">
              {{ row.label }}
              <code class="ml-1 rounded bg-slate-100 px-1 font-mono text-[11px] text-slate-500">
                {{ row.field }}
              </code>
            </dt>
            <dd class="mt-0.5 break-all font-mono text-slate-800">
              <el-tooltip v-if="row.hint !== null" :content="row.hint" placement="top">
                <span class="cursor-help underline decoration-slate-300 decoration-dotted">
                  {{ row.value }}
                </span>
              </el-tooltip>
              <span v-else>{{ row.value }}</span>
            </dd>
          </div>
        </dl>

        <p v-if="!hasTenantPolicy" class="mt-3 text-xs text-slate-400">
          这次响应没有 <code>tenant_policy</code>：只有经套餐门禁解析的调用才有这一栏。
        </p>
        <p v-else class="mt-3 text-xs text-slate-400">
          <code>tenant_policy</code> 是「本次实际生效」的唯一真相：档位想要的重排 / 改写
          可能被部署能力或套餐上限裁掉，以上面显示的值为准。
        </p>
      </div>
    </el-card>
  </section>
</template>
