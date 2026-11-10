<script setup lang="ts">
/**
 * 检索页：选 Key → 勾库 → 发检索 → 结果卡片 + 只读运行摘要。
 *
 * 四条不许越过的线（issue #87 / #92 / spec #82）：
 * 1. 请求体只有 `kb_ids` + `query`：本页没有任何档位按钮或高级参数输入，
 *    `profile` / `top_k` / `mode` / `rerank` / `rewrite` 一律不进请求体；
 * 2. 运行摘要只读响应 `metadata`，**不看页面输入**——摘要回答的是「后端这次实际
 *    生效了什么」，拿页面配置冒充它就等于把调试台变成许愿台；
 * 3. 身份只有「选中的那把 Key」：本页是全仓库唯一的数据面入口的使用者
 *    （`ragService.retrieve()`），未选 Key 时请求层会抛可操作的错误；
 * 4. 可勾选的库来自**控制面树**（会话身份），但只有当前 Key 的 `allowed_kb_ids`
 *    白名单里的库能勾选；白名单外的置灰 + hover 说明原因，换 Key 时自动摘掉不在
 *    新白名单里的已选库并提示（留着它们只会在下一次检索撞 20008）。
 *
 * 结果卡片只展示契约里有的字段；`latency_ms` / `log_id` / `trace_id` 等字段后端没有
 * （00-facts §5 明确列过），这里也不编造。
 */
import { useMutation, useQuery } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import ComparePanel from '../components/ComparePanel.vue'
import PageHeader from '../components/PageHeader.vue'
import RunSummary from '../components/RunSummary.vue'
import { ApiError } from '../lib/envelope'
import { KEYS_QUERY_KEY, fetchKeys } from '../services/keyService'
import { KNOWLEDGE_BASE_TREE_QUERY_KEY, fetchTree } from '../services/knowledgeBaseService'
import { retrieve } from '../services/ragService'
import { useRetrieveStore } from '../stores/retrieve'
import type { KbOption, KeyData } from '../types/key'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'
import { pushHistory, type CompareHistoryEntry } from '../utils/retrieveCompare'
import {
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

// —— ① 选 Key 与检索范围 ——

/**
 * `?kb_id=` 预填。同样的坑上传页已经踩过：同一路由只改 query 不会重新挂载组件，
 * 只读一次会让勾选停在上一个库，所以初次读一次、之后再 watch。
 * 预填发生在选 Key 之前时可能落在空里（不在任何白名单）；那是「未选 Key」的正常状态，
 * 选 Key 之后由 store 的白名单裁剪统一处理。
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

// 本页自己的缓存键：与 /keys、列表页共用同一份 Key / 树缓存，失效时一起更新。
const keysQuery = useQuery({
  queryKey: KEYS_QUERY_KEY,
  queryFn: fetchKeys,
  retry: false,
})
const keys = computed<KeyData[]>(() => keysQuery.data.value ?? [])
const keysPending = keysQuery.isPending
const keysLoaded = keysQuery.isSuccess
const keysError = computed(() =>
  keysQuery.error.value === null ? '' : errorText(keysQuery.error.value),
)

const treeQuery = useQuery({
  queryKey: KNOWLEDGE_BASE_TREE_QUERY_KEY,
  queryFn: fetchTree,
  retry: false,
})
const kbs = computed<KbOption[]>(() =>
  (treeQuery.data.value ?? []).map((node) => ({ kb_id: node.kb_id, name: node.name })),
)
const treePending = treeQuery.isPending
const treeLoaded = treeQuery.isSuccess
const treeError = computed(() =>
  treeQuery.error.value === null ? '' : errorText(treeQuery.error.value),
)

const selectedKey = computed<KeyData | null>(
  () => keys.value.find((key) => key.key_id === store.selectedKeyId) ?? null,
)
const policy = computed(() => selectedKey.value?.policy ?? null)
const allowedKbIds = computed(() => new Set(policy.value?.allowed_kb_ids ?? []))
const knownKbIds = computed(() => new Set(kbs.value.map((kb) => kb.kb_id)))

/**
 * 把选中的 Key 同步进 store（id + 原文 + 白名单），并把白名单外的已选库摘掉。
 *
 * 为什么在 watch 里也调一次：`/keys` 改完白名单回到本页时，`['keys']` 会重新拉取，
 * 同一把 Key 的白名单可能已经变了；只靠「用户手动切 Key」不会重算，于是已选库里会留着
 * 一个刚被移除授权的库，下一次检索才会以 20008 失败。
 */
function syncSelectedKey(key: KeyData): void {
  const removed = store.selectKey({
    keyId: key.key_id,
    keyValue: key.key_value,
    allowedKbIds: key.policy?.allowed_kb_ids ?? [],
  })
  if (removed.length > 0) {
    ElMessage.warning(`这些库不在新 Key 的白名单里，已移出选择：${removed.join('、')}`)
  }
}

watch(
  selectedKey,
  (key) => {
    if (key === null) return
    // 只在「页面认定的那把」与 store 里的一致时同步：切换 Key 由 onKeyChange 负责，
    // 这里管的是「同一把 Key 的数据刷新了」（例如刚在 /keys 改过白名单）。
    if (key.key_id !== store.selectedKeyId) return
    syncSelectedKey(key)
  },
  { immediate: true },
)

// 之前选中的 Key 被删掉（例如在 /keys 删了它）：清掉选择并说明原因，别让下一次检索
// 拿一把已删除的 Key 去撞 20010（那个码看起来像「Key 失效」）。
watch(
  [keys, keysLoaded],
  ([rows, loaded]) => {
    if (!loaded || !store.selectedKeyId) return
    if (rows.some((key) => key.key_id === store.selectedKeyId)) return
    ElMessage.warning('之前选中的 Key 已不在列表里（可能已被删除），请重新选择一把')
    store.clearKey()
  },
  { immediate: true },
)

/**
 * 只有一把 Key 时直接选上。
 *
 * 与上传页「清单里只有一个库就不再让用户点一次」是同一条规矩：这里点一次没有任何
 * 信息量，而不选就没法检索——多出来的这一次点击纯粹是摩擦。两把以上**不猜**：
 * 那时候选哪把是有意义的决定。
 *
 * 条件里的 `store.selectedKeyId === null` 保证它不会覆盖用户的选择（含从别处带过来的）。
 */
watch(
  [keys, keysLoaded],
  ([rows, loaded]) => {
    if (!loaded) return
    if (store.selectedKeyId !== null) return
    if (rows.length !== 1) return
    syncSelectedKey(rows[0])
  },
  { immediate: true },
)

function onKeyChange(value: unknown): void {
  const keyId = typeof value === 'string' ? value : null
  if (keyId === null || keyId === '') {
    store.clearKey()
    return
  }
  const key = keys.value.find((item) => item.key_id === keyId)
  if (key === undefined) return
  syncSelectedKey(key)
}

/**
 * 选中 Key 的**只读**配置摘要：档位 / 能力 / 限额 / 结果形状。
 *
 * 这里**故意不列数据范围**：那把 Key 圈了哪些库，下方「勾选知识库」逐条摆着同一份
 * 信息（名字 + id + 能不能勾）。同一件事在一屏里写两遍，读者要花时间确认「这两处
 * 是不是一回事」——去重比补全更重要。
 */
const policyRows = computed(() => {
  const value = policy.value
  if (value === null) return []
  const capabilities: string[] = []
  if (value.research_allowed) capabilities.push('research')
  if (value.hybrid_allowed) capabilities.push('hybrid')
  if (value.rerank_allowed) capabilities.push('rerank')
  if (value.query_rewrite_allowed) capabilities.push('query_rewrite')
  return [
    { label: '档位', value: value.profile },
    {
      label: '生效能力',
      value: capabilities.length === 0 ? '全关' : capabilities.join(' / '),
    },
    { label: '限额', value: `${value.retrieve_qps} 次/秒 · ${value.retrieve_daily} 次/天` },
    {
      // 用**概念名**而不是请求体字段名：这两个开关在界面上叫 small-to-big / rows-to-table
      // （与 Key 编辑器同一套说法）。一屏里两处用不同名字，用户会以为它们是两个东西。
      label: '形状',
      value: `${value.include_parent ? 'small-to-big' : '不折叠'} · ${
        value.expand_table_rows ? 'rows-to-table' : '表格行不折回'
      }`,
    },
  ]
})

/**
 * 已选但这一次勾不上的库（例如 URL 预填了一个不在这把 Key 白名单里的旧库 ID）。
 *
 * 不静默删掉它们：用户可能正想查一个不在这把 Key 范围里的库；但必须提前说明检索
 * 大概率会失败，并给一个「移除」的动作。
 */
const unpickableSelected = computed(() =>
  store.selectedKbIds.filter(
    (kbId) => !knownKbIds.value.has(kbId) || !allowedKbIds.value.has(kbId),
  ),
)

function dropUnpickable(): void {
  store.setSelection(
    store.selectedKbIds.filter(
      (kbId) => knownKbIds.value.has(kbId) && allowedKbIds.value.has(kbId),
    ),
  )
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
  if (selectedKey.value === null) return '先在上方选一把 Key（它决定能查哪些库、用哪档配置）'
  if (policy.value === null) return '这把 Key 没有可用的密钥配置：去 /keys 编辑配置或换一把'
  if (store.selectedCount === 0) return '先在上方勾选至少一个知识库'
  if (trimmedQuery.value === '') return '先输入要检索的问题（全空白不算问题）'
  return ''
})

// —— ③ 结果 / ④ 摘要 ——
const result = ref<RetrieveData | null>(null)
const runError = ref<{ message: string; limitHint: boolean } | null>(null)

// —— ⑤ 对比历史（只在内存） ——
/**
 * 为什么只活在组件 `ref` 里：对比是排障时「刚刚改了什么」的会话内手段，不是持久化
 * 报表 —— 刷新 / 退出页面即清空，**刻意不写 localStorage/sessionStorage**。
 * 上一次会话的基准拿来对照新的响应，会把「配置其实变了」看成「没变」，恰好与本面板
 * 的用途相反。上限 5 由 `pushHistory` 保证：超出丢最旧，留下的都是「刚刚」。
 */
const history = ref<CompareHistoryEntry[]>([])
const pinnedSeq = ref<number | null>(null)
/** 会话内的检索序号；从 1 开始，只进内存，不进 URL / storage。 */
let nextSeq = 1

function pinBase(seq: number): void {
  pinnedSeq.value = seq
}

function unpinBase(): void {
  pinnedSeq.value = null
}

const { mutate, isPending: running } = useMutation({
  mutationFn: (payload: RetrieveRequest) => retrieve(payload),
  onMutate: () => {
    runError.value = null
  },
  onSuccess: (data) => {
    result.value = data
    // 只有成功的检索才进历史：失败没有 `RetrieveData` 可对比，硬造一条空记录会让
    // 「第 N 次」名不副实，也会把基准的编号整体错位。
    const seq = nextSeq
    nextSeq += 1
    // 只存响应引用本身：`result` 与历史都只读这份数据，页面从不修改它，
    // 所以两份引用指向同一份快照是安全的（真去深拷贝一遍只会多一份可能被改的副本）。
    history.value = pushHistory(history.value, {
      seq,
      at: new Date().toISOString(),
      data,
    })
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

// 摘要的渲染整体在 `RunSummary.vue` 里（纯 props，只收 metadata）：页面不自己摊
// `metadata`，也就不可能顺手把页面输入混进摘要。
</script>

<template>
  <section class="space-y-6">
  <PageHeader
    title="调试台"
    subtitle="选一把 Key、勾要查的库、发一次真实检索。检索方式、条数、重排与改写都由这把 Key 的密钥配置决定——本页不设这些参数，跑完只看后端回显的真实生效值。"
  />
    <!-- ① 选 Key 与检索范围 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <h2 class="text-sm font-semibold text-ink">检索范围</h2>
          <span class="shrink-0 text-[11px] text-muted">
            已选 {{ store.selectedCount }} / 共 {{ kbs.length }} 个库
          </span>
        </div>
      </template>

      <!-- 选 Key -->
      <p v-if="keysPending" class="text-sm text-slate-400">正在读取 Key 列表…</p>
      <div
        v-else-if="keysError !== ''"
        class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-sm text-[#6E4E12]"
      >
        {{ keysError }}
      </div>
      <div
        v-else-if="keys.length === 0"
        class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-sm text-[#6E4E12]"
      >
        还没有任何 API Key：先去
        <RouterLink to="/keys" class="text-brand underline">Key 管理页</RouterLink>
        签发一把，再回来选它。
      </div>
      <template v-else>
        <!--
          Key 行的两处调整：
          1. 「去改这把 Key 的配置」从 select 右侧挪到标签行右端。原来它与 select 底对齐，
            贴着控件外沿，看起来不像这一行的动作，倒像溢出到卡片外的散字。
          2. 配置摘要从两列网格压成一行内联文本。两列时标签与值是同色同重的 11px 灰，
            「档位 quality」读起来像一个词；而且 3 + 2 两项排下来高度不齐，右列吊在半空。
        -->
        <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <label class="text-[11px] text-muted">当前选中的 Key</label>
          <RouterLink
            v-if="selectedKey !== null"
            :to="{ path: '/keys', query: { key_id: selectedKey.key_id } }"
            class="shrink-0 text-[11px] text-brand underline underline-offset-2 hover:text-accent-hover"
          >
            去改这把 Key 的配置 →
          </RouterLink>
        </div>
        <el-select
          :model-value="store.selectedKeyId ?? undefined"
          class="mt-1.5 w-full"
          clearable
          filterable
          placeholder="选择一把 Key 来发起检索"
          @change="onKeyChange"
        >
          <el-option
            v-for="key in keys"
            :key="key.key_id"
            :value="key.key_id"
            :label="`${key.label ?? '（未命名）'} · ${key.key_masked}`"
          >
            <span class="font-mono text-xs">{{ key.label ?? '（未命名）' }}</span>
            <span class="ml-2 text-xs text-slate-400">{{ key.key_masked }}</span>
            <span v-if="key.policy === null" class="ml-2 text-xs text-[#6E4E12]">
              配置缺失
            </span>
          </el-option>
        </el-select>

        <div
          v-if="policy === null && selectedKey !== null"
          class="mt-2.5 rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-xs leading-relaxed text-[#6E4E12]"
        >
          这把 Key 没有绑定的密钥配置（可能被删除了）：它连「查哪些库」都没有答案，去
          <RouterLink
            :to="{ path: '/keys', query: { key_id: selectedKey.key_id } }"
            class="text-brand underline"
          >
            Key 管理页
          </RouterLink>
          重新签发一把，或选中另一把。
        </div>

        <!-- 配置摘要：一行内联，标签淡、值深，扫一眼就够。 -->
        <div
          v-else-if="policy !== null"
          class="mt-2.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-control bg-surface-muted px-3 py-2 text-[11px] leading-relaxed"
        >
          <span v-for="(row, index) in policyRows" :key="row.label" class="flex items-baseline">
            <span class="text-muted">{{ row.label }}</span>
            <span class="ml-1 font-mono text-slate-700">{{ row.value }}</span>
            <span v-if="index < policyRows.length - 1" class="mx-2 text-line-strong">·</span>
          </span>
        </div>
      </template>

      <!-- 勾库 -->
      <p v-if="treePending" class="mt-5 text-sm text-slate-400">正在读取本租户的知识库…</p>
      <div
        v-else-if="treeError !== ''"
        class="mt-5 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
      >
        {{ treeError }}
      </div>
      <div
        v-else-if="kbs.length === 0"
        class="mt-5 rounded-control border border-line bg-surface-muted px-3 py-2 text-sm text-slate-600"
      >
        本租户还没有知识库：先去创建知识库并上传文档，再回来检索。
        <RouterLink to="/" class="text-brand underline">去建库 / 上传文档</RouterLink>
      </div>
      <el-checkbox-group
        v-else
        class="mt-4 grid gap-2 sm:grid-cols-2"
        :model-value="store.selectedKbIds"
        @update:model-value="onSelectionChange"
      >
        <el-tooltip
          v-for="kb in kbs"
          :key="kb.kb_id"
          :disabled="allowedKbIds.has(kb.kb_id)"
          content="这个库不在当前 Key 的 allowed_kb_ids 白名单里：去 /keys 改这把 Key 的配置后才能勾选"
          placement="top"
        >
          <el-checkbox :value="kb.kb_id" :disabled="!allowedKbIds.has(kb.kb_id)" class="!mr-0">
            <span class="text-sm text-slate-700">{{ kb.name }}</span>
            <!-- id 用更小更淡的字 + 前置分隔点：不加分隔时「kb 名字」与 UUID 会连成一片。 -->
            <span class="ml-2 font-mono text-[10px] text-slate-400">· {{ kb.kb_id }}</span>
          </el-checkbox>
        </el-tooltip>
      </el-checkbox-group>

      <div
        v-if="treeLoaded && unpickableSelected.length > 0"
        class="mt-3 rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-xs text-[#6E4E12]"
      >
        <p>这些已选库不在当前 Key 的白名单里（或不在这棵树的可见范围里），带它们检索大概率会失败：</p>
        <p class="mt-1 break-all font-mono">{{ unpickableSelected.join('、') }}</p>
        <el-button class="mt-2" size="small" @click="dropUnpickable">移除这些选择</el-button>
      </div>
    </el-card>

    <!--
      ② 问题。原来标题下还有一句「请求体只有 kb_ids 与 query…」——页头已经说了「本页不设
      这些参数」，这句是把同一件事换个说法再说一遍。删掉，只留标题与一次动作。
    -->
    <el-card shadow="never">
      <template #header>
        <h2 class="text-sm font-semibold text-ink">问题</h2>
      </template>

      <el-form label-position="top" @submit.prevent="submitRetrieve">
        <el-form-item>
          <el-input
            v-model="queryText"
            type="textarea"
            :rows="3"
            maxlength="2000"
            show-word-limit
            placeholder="要检索的问题（可多行）· 例如：退款申请提交后，一般几天能到账？"
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
          <span v-if="blockReason !== ''" class="text-xs text-muted">{{ blockReason }}</span>
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
        还没发起检索：先选 Key、勾库、写问题，再点「检索」。
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
            <!--
              这里是 `expansion`（这次上下文给了多大范围：neighbors / parent / table / chunk），
              不是「块类型」。GLOSSARY 里「块类型」是切片形态（prose / list / table / table_row /
              code），而响应里根本没有这个字段。两者一旦混用，读结果的人会以为切片被解析成了
              “parent” 这种类型。
            -->
            <span>扩展范围：{{ chunk.expansion ?? '—' }}</span>
          </div>
        </li>
      </ul>
    </el-card>

    <!-- ⑤ 对比面板（会话内，放在结果区下方；没有历史时整块不渲染，不占位） -->
    <ComparePanel
      v-if="history.length > 0"
      :history="history"
      :pinned-seq="pinnedSeq"
      @pin="pinBase"
      @unpin="unpinBase"
    />

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

      <RunSummary v-else :metadata="result.metadata" />
    </el-card>
  </section>
</template>
