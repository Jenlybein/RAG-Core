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
  if (selectedKey.value === null) return '还没选 Key —— 点「修改配置」选一把'
  if (policy.value === null) return '这把 Key 没有可用的密钥配置 —— 去 Key 管理页改一下'
  if (store.selectedCount === 0) return '还没勾库 —— 点「修改配置」勾选至少一个知识库'
  if (trimmedQuery.value === '') return '先写要检索的问题（全空白不算问题）'
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

/**
 * UUID 只显示前 8 位。
 *
 * 结果列表里两条 36 字符的 UUID 会各占半行，而它们几乎从不是阅读目标——需要的时候
 * 悬停看全长即可。这一条同时把「切片 ID / 文档 ID」从主行降到底部小字。
 */
function shortId(value: string): string {
  return value.slice(0, 8)
}

/** 运行摘要默认收起：它是调试元信息，第一次读结果的人不需要它。 */
const summaryOpen = ref(false)

/**
 * 「修改配置」弹窗。选 Key 这件事被收进弹窗之后，卡片上只留一行只读摘要。
 *
 * 为什么这样分：选 Key 与改 Key 的配置属于「**设置**」，而勾库、写问题、点检索是
 * 「**动作**」。设置是做一次就不再碰的东西，把它常驻在动作流的最上方，会让每次检索
 * 都先经过一个与本次无关的下拉框。
 */
const configDialogOpen = ref(false)

/** 卡片上那一行只读摘要：当前这把 Key 是谁、档位是什么。 */
const currentKeySummary = computed(() => {
  if (selectedKey.value === null) return null
  return {
    label: selectedKey.value.label ?? '（未命名）',
    masked: selectedKey.value.key_masked,
    profile: policy.value?.profile ?? null,
  }
})

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
    subtitle="选 Key、勾库、写问题，发一次真实检索。检索方式、条数、重排与改写全部由这把 Key 的密钥配置决定，本页只回显后端实际生效的值。"
  />
    <!-- ① 选 Key 与检索范围 -->
    <!--
      卡片只留「动作」：读一行当前配置、写问题、点检索。
      选 Key、改它的配置、勾库都属于「设置」——设置做一次就不再碰，全部收进
      「修改配置」弹窗；常驻在动作流最上方，会让每次检索都先经过一组与本次无关的控件。
    -->
    <el-card shadow="never">
      <template #header>
        <h2 class="text-sm font-semibold text-ink">发起检索</h2>
      </template>

      <!-- 一行只读摘要。不显示的话，用户每次检索前都得开弹窗确认用的是哪把 Key、哪些库。 -->
      <p v-if="keysPending" class="text-[11px] text-muted">正在读取 Key 列表…</p>
      <div
        v-else-if="keysError !== ''"
        class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-[11px] text-[#6E4E12]"
      >
        {{ keysError }}
      </div>
      <div
        v-else-if="keys.length === 0"
        class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-[11px] leading-relaxed text-[#6E4E12]"
      >
        还没有任何 API Key：先去
        <RouterLink to="/keys" class="text-brand underline">Key 管理页</RouterLink>
        签发一把。
      </div>
      <div v-else class="rounded-control bg-surface-muted px-3 py-2.5">
        <div
          class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[11px]"
        >
          <span class="flex flex-wrap items-baseline gap-2">
            <span class="text-muted">当前 Key</span>
            <template v-if="currentKeySummary !== null">
              <span class="font-mono text-[12px] font-medium text-ink">
                {{ currentKeySummary.label }}
              </span>
              <span class="font-mono text-[10px] text-slate-400">
                {{ currentKeySummary.masked }}
              </span>
              <span class="text-line-strong">·</span>
              <span :class="currentKeySummary.profile === null ? 'text-[#6E4E12]' : 'text-muted'">
                档位 {{ currentKeySummary.profile ?? '缺失' }}
              </span>
            </template>
            <span v-else class="text-[#6E4E12]">未选择 —— 点「修改配置」选一把</span>
          </span>
          <span class="text-muted">已选 {{ store.selectedCount }} 个库</span>
        </div>
      </div>

      <div class="mt-5 border-t border-line pt-5">
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
          <!--
            动作行右对齐：左端是「为什么现在点不了」，右端是这一卡的两个出口。
            「修改配置」在「检索」左边——改配置是为检索服务的准备动作，主按钮永远在最右。
          -->
          <div class="flex flex-wrap items-center justify-end gap-2">
            <span v-if="blockReason !== ''" class="mr-auto text-[11px] text-muted">
              {{ blockReason }}
            </span>
            <el-button :disabled="keys.length === 0" @click="configDialogOpen = true">
              修改配置
            </el-button>
            <el-button
              type="primary"
              native-type="submit"
              :disabled="blockReason !== '' || running"
              :loading="running"
            >
              检索
            </el-button>
          </div>
        </el-form>
      </div>
    </el-card>

    <!--
      「修改配置」弹窗：这张卡的所有「设置」都在这儿——选 Key、读它的密钥配置、勾要查的库。
      改动立即生效（change 即同步 store），所以底部只有「关闭」：这里没有「保存」这回事，
      多一个确认按钮反而让人以为刚才的选择还没落地。
    -->
    <el-dialog
      v-model="configDialogOpen"
      append-to-body
      title="修改检索配置"
      width="600px"
    >
      <div class="space-y-5 text-sm">
        <!-- ① Key -->
        <div>
          <label class="mb-1.5 block text-[11px] text-muted">当前选中的 Key</label>
          <p v-if="keysPending" class="text-[11px] text-muted">正在读取 Key 列表…</p>
          <p v-else-if="keys.length === 0" class="text-[11px] text-[#6E4E12]">
            还没有任何 API Key：先去
            <RouterLink to="/keys" class="text-brand underline">Key 管理页</RouterLink>
            签发一把。
          </p>
          <el-select
            v-else
            :model-value="store.selectedKeyId ?? undefined"
            class="w-full"
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
        </div>

        <div
          v-if="policy === null && selectedKey !== null"
          class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-[11px] leading-relaxed text-[#6E4E12]"
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

        <div v-else-if="policy !== null">
          <p class="mb-1.5 text-[11px] text-muted">这把 Key 的密钥配置（只读）</p>
          <dl
            class="grid gap-x-6 gap-y-2 rounded-control bg-surface-muted px-3 py-2.5 text-[11px] sm:grid-cols-2"
          >
            <div v-for="row in policyRows" :key="row.label" class="flex flex-col gap-0.5">
              <dt class="text-muted">{{ row.label }}</dt>
              <dd class="break-all font-mono text-slate-700">{{ row.value }}</dd>
            </div>
          </dl>
        </div>

        <!-- ② 要查的知识库 -->
        <div class="border-t border-line pt-4">
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <span class="text-[11px] font-medium text-ink">要查的知识库</span>
            <span class="text-[11px] text-muted">
              已选 {{ store.selectedCount }} / 共 {{ kbs.length }}
            </span>
          </div>
          <p class="mt-1 text-[11px] leading-relaxed text-muted">
            只能勾这把 Key 白名单里的库；置灰的那些不在白名单里，去 Key 管理页改配置后才能勾。
          </p>

          <p v-if="treePending" class="mt-2.5 text-[11px] text-muted">
            正在读取本租户的知识库…
          </p>
          <div
            v-else-if="treeError !== ''"
            class="mt-2.5 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700"
          >
            {{ treeError }}
          </div>
          <div
            v-else-if="kbs.length === 0"
            class="mt-2.5 rounded-control border border-line bg-surface-muted px-3 py-2 text-[11px] text-slate-600"
          >
            本租户还没有知识库：先去创建知识库并上传文档，再回来检索。
            <RouterLink to="/" class="text-brand underline">去建库 / 上传文档</RouterLink>
          </div>
          <el-checkbox-group
            v-else
            class="mt-2.5 grid gap-2 sm:grid-cols-2"
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
              <!-- 可点的小卡而不是裸复选框：有边框才读得出「这是一个可以点的对象」。 -->
              <el-checkbox
                :value="kb.kb_id"
                :disabled="!allowedKbIds.has(kb.kb_id)"
                class="kb-option !mr-0"
                :class="{
                  'kb-option--on': store.selectedKbIds.includes(kb.kb_id),
                  'kb-option--off': !allowedKbIds.has(kb.kb_id),
                }"
              >
                <span class="flex flex-col leading-tight">
                  <span class="text-[13px] text-ink">{{ kb.name }}</span>
                  <span class="mt-0.5 font-mono text-[10px] text-slate-400">{{ kb.kb_id }}</span>
                </span>
              </el-checkbox>
            </el-tooltip>
          </el-checkbox-group>

          <div
            v-if="treeLoaded && unpickableSelected.length > 0"
            class="mt-3 rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-[11px] leading-relaxed text-[#6E4E12]"
          >
            <p>这些已选库不在当前 Key 的白名单里（或不在这棵树的可见范围里），带它们检索大概率会失败：</p>
            <p class="mt-1 text-slate-700">{{ unpickableSelected.join('、') }}</p>
            <el-button class="mt-2" size="small" @click="dropUnpickable">移除这些选择</el-button>
          </div>
        </div>
      </div>

      <template #footer>
        <RouterLink
          v-if="selectedKey !== null"
          :to="{ path: '/keys', query: { key_id: selectedKey.key_id } }"
          class="mr-auto text-[11px] text-brand underline underline-offset-2"
        >
          去 Key 管理页改配置 →
        </RouterLink>
        <el-button @click="configDialogOpen = false">关闭</el-button>
      </template>
    </el-dialog>

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
        <!--
          每条结果重排成「主行 / 正文 / 底注」三段。旧版把 5 个分数指标铺成两行 dl、
          再把两个 UUID 铺成一行，整块看下来是**一堆元数据而没有正文**——`chunk.content`
          后端一直在返回，却从没被渲染过。人来做检索是为了看命中什么，不是看分数表。
        -->
        <li v-for="(chunk, index) in chunks" :key="chunk.chunk_id" class="py-5">
          <div class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <div class="flex min-w-0 items-baseline gap-2.5">
              <span
                class="shrink-0 rounded-full bg-accent-soft px-2 py-0.5 font-mono text-[11px] font-medium text-ink"
              >
                {{ index + 1 }}
              </span>
              <h3 class="truncate text-[13px] font-semibold text-ink">
                {{ chunk.title || '（无标题）' }}
              </h3>
              <span v-if="chunk.heading_path !== null" class="truncate text-[11px] text-muted">
                › {{ chunk.heading_path }}
              </span>
            </div>

            <!-- 分数只留三个：主排序分大、两路分解小。名次留在底注。 -->
            <div class="flex shrink-0 items-baseline gap-3">
              <span class="font-mono text-[13px] font-medium text-ink">
                {{ formatScore(chunk.score) }}
              </span>
              <span class="font-mono text-[10px] text-muted">
                语义 {{ formatPercent(chunk.vector_score) }}
              </span>
              <span class="font-mono text-[10px] text-muted">
                关键词 {{ formatScore(chunk.bm25_score) }}
              </span>
            </div>
          </div>

          <!-- 命中正文。保留换行与空白：切片的形状（列表、表格行）是它的一半信息。 -->
          <p class="mt-2.5 whitespace-pre-wrap break-words text-[13px] leading-[1.8] text-slate-700">
            {{ chunk.content }}
          </p>

          <div class="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-slate-400">
            <span v-if="chunk.expansion !== null">扩展：{{ chunk.expansion }}</span>
            <span>语义名次 {{ formatRank(chunk.vector_rank) }}</span>
            <span>关键词名次 {{ formatRank(chunk.bm25_rank) }}</span>
            <span :title="chunk.chunk_id">切片 {{ shortId(chunk.chunk_id) }}</span>
            <span :title="chunk.document_id">文档 {{ shortId(chunk.document_id) }}</span>
            <span v-if="chunk.kb_id !== null" :title="chunk.kb_id">库 {{ shortId(chunk.kb_id) }}</span>
          </div>
        </li>
      </ul>

      <!--
        运行摘要收进结果卡底部、默认收起。它原来自己占一张卡，与结果并列——但它是
        「这次为什么这样跑」的元信息，读结果的当下不需要，需要一个入口就够了。
      -->
      <div class="mt-4 border-t border-line pt-3">
        <button
          type="button"
          class="flex items-center gap-1.5 text-[11px] text-muted transition hover:text-ink"
          @click="summaryOpen = !summaryOpen"
        >
          <span class="text-[9px]">{{ summaryOpen ? '▼' : '▶' }}</span>
          运行摘要 · 这次后端实际生效的值
        </button>
        <RunSummary v-if="summaryOpen && result !== null" class="mt-3" :metadata="result.metadata" />
        <p v-if="summaryOpen && result === null" class="mt-2 text-[11px] text-muted">
          还没有响应：跑一次检索后，这里显示后端这次实际生效的值。
        </p>
      </div>
    </el-card>

    <!-- ④ 对比面板（会话内，放在结果区下方；没有历史时整块不渲染，不占位） -->
    <ComparePanel
      v-if="history.length > 0"
      :history="history"
      :pinned-seq="pinnedSeq"
      @pin="pinBase"
      @unpin="unpinBase"
    />
  </section>
</template>
