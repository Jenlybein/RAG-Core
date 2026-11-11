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
 * 结果卡片只展示契约里有的字段；`latency_ms` / `log_id` 后端没有（00-facts §5 明确列过），
 * 这里也不编造。`metadata.trace_id` 从 #101 起有了，但本页不摊它——它在这张卡上的用途只有
 * 一个：与瀑布图里那根线对上（见下方「六段耗时」面板的说明）。
 */
import { useMutation, useQuery } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import ComparePanel from '../components/ComparePanel.vue'
import PageHeader from '../components/PageHeader.vue'
import RunSummary from '../components/RunSummary.vue'
import StageWaterfall from '../components/StageWaterfall.vue'
import { ApiError } from '../lib/envelope'
import { fetchWaterfall, resolveLogId } from '../services/consoleTraces'
import { submitFeedback } from '../services/feedbackService'
import {
  fetchSideEvidence,
  fetchSideEvidenceReport,
  fetchSideEvidenceRuns,
  triggerSideEvidence,
  type SideEvidenceRun,
} from '../services/sideEvidenceService'
import { KEYS_QUERY_KEY, fetchKeys } from '../services/keyService'
import { KNOWLEDGE_BASE_TREE_QUERY_KEY, fetchTree } from '../services/knowledgeBaseService'
import { retrieve } from '../services/ragService'
import { fetchRuntimeInfo, runtimeInfoQueryKey } from '../services/traceService'
import { useRetrieveStore } from '../stores/retrieve'
import type { KbOption, KeyData } from '../types/key'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'
import type { RetrievalWaterfall } from '../types/traceWaterfall'
import { feedbackErrorCopy, isValidScore, scoreStars } from '../utils/feedback'
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

/**
 * 服务端是不是「开发身份模式」。读不到就当作**开着**（不显示警告）：
 * 这条提示只是预防性的，读失败时弹一条红字反而会盖掉真正的问题。
 */
const runtimeQuery = useQuery({
  queryKey: runtimeInfoQueryKey(),
  queryFn: fetchRuntimeInfo,
  retry: false,
})
const authDisabled = computed(() => runtimeQuery.data.value?.auth_enabled === false)

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
    // 这一次的线换掉了：重新反查 log_id，评分控件随之绑定到**这次**结果。
    // 反查失败不报错弹窗（那只是「不能打分」），由控件里的文案说明。
    void prepareRating(data.metadata.trace_id)
  },
  onError: (error) => {
    // 本次失败就清掉上一次的结果：旧卡片配新报错会让人以为「这次也成功了一部分」，
    // 而摘要更必须永远对应最近一次响应，不能停在上一次。
    result.value = null
    ratingLogId.value = null
    ratingSaved.value = null
    ratingScore.value = 0
    ratingError.value = null
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

// —— ⑥ 六段耗时（瀑布图，#103） ——
/**
 * 一个输入框 + 一个按钮，按 `log_id` 去控制面读回六段耗时。
 *
 * 优先用**这次检索的 `log_id`**（与评分控件同一个反查结果，见下方 `ratingLogId`）；反查没
 * 成时退回手填。手填这条路径保留下来是给「报障时只拿到一个工单里的 `log_id`」用的——
 * 那时页面根本没有这一次的响应，也就没有可反查的 `trace_id`。
 *
 * 当初只能手填，是因为 `trace_id` 是 `log_id` 的单向派生、推不回去；而控制面的反查端点
 * （#106 起）把这条补上了，code-review 之后这里才接上去。
 *
 * 失败与「未启用」分开处理：前者是 `waterfallError`（业务码原样带上），后者是
 * `code=0` 的正常响应，由 `StageWaterfall` 按 `reason` 说清是哪一种。
 */
const waterfallOpen = ref(false)
const logIdInput = ref('')
const waterfall = ref<RetrievalWaterfall | null>(null)
const waterfallError = ref<string | null>(null)
const trimmedLogId = computed(() => logIdInput.value.trim())

const { mutate: requestWaterfall, isPending: waterfallLoading } = useMutation({
  mutationFn: (logId: string) => fetchWaterfall(logId),
  onMutate: () => {
    waterfallError.value = null
  },
  onSuccess: (data) => {
    waterfall.value = data
  },
  onError: (error) => {
    // 上一次的瀑布必须一起清掉：留着它会让「查不到这个 log_id」看起来像查到了另一条。
    waterfall.value = null
    waterfallError.value = errorText(error)
  },
})

function loadWaterfall(): void {
  if (waterfallLoading.value) return
  // 优先用「这次检索」那一个（与评分控件同一个反查结果）；没有响应时才读手填框。
  const target = ratingLogId.value ?? trimmedLogId.value
  if (target === '') return
  requestWaterfall(target)
}

// —— ⑦ 给这次检索打分（#106） ——
/**
 * 为什么不用手填 `log_id`：检索响应里有 `metadata.trace_id`（#101），控制面能按它反查
 * 回 `log_id`。让人再抄一遍 36 位 UUID，等于把「把分挂到别的检索上」做成一个正常操作。
 *
 * 三步都不碰观测开关：反查读的是归属索引，提交写的是本地反馈表；观测栈全挂时
 * 打分照样成功——这正是「反馈以本地为准」（#105）的意义。
 */
const ratingScore = ref(0)
const ratingComment = ref('')
const ratingLogId = ref<string | null>(null)
const ratingError = ref<string | null>(null)
const ratingSaved = ref<number | null>(null)
const ratingResolving = ref(false)
const ratingSubmitting = ref(false)

/** 当前这次检索的 `trace_id`（响应里永远有值）。换一次检索就换一根线。 */
const currentTraceId = computed(() => result.value?.metadata.trace_id ?? null)

const ratingDisabledCopy = computed(() => {
  if (result.value === null) return '先发一次检索，再给这次结果打分。'
  if (currentTraceId.value === null) return '这次响应没有 trace_id，无法定位到那一次检索。'
  if (ratingResolving.value) return '正在定位这次检索…'
  if (ratingLogId.value === null) return '这次检索记录不存在或不属于当前账号，不能打分。'
  return ''
})

/** 每次成功检索都重新反查，并清掉上一轮的评分状态。 */
async function prepareRating(traceId: string): Promise<void> {
  ratingResolving.value = true
  ratingLogId.value = null
  ratingError.value = null
  ratingSaved.value = null
  ratingScore.value = 0
  ratingComment.value = ''
  try {
    const lookup = await resolveLogId(traceId)
    // 只接受「当前这根线」的答案：请求在途时用户又发了一次检索，旧响应不能覆盖新状态。
    if (currentTraceId.value === traceId) {
      ratingLogId.value = lookup.log_id
    }
  } catch (error) {
    if (currentTraceId.value === traceId) {
      ratingError.value = errorText(error)
    }
  } finally {
    ratingResolving.value = false
  }
}

async function sendRating(): Promise<void> {
  const logId = ratingLogId.value
  const traceId = currentTraceId.value
  if (logId === null || traceId === null || ratingSubmitting.value) return
  if (!isValidScore(ratingScore.value)) {
    ratingError.value = feedbackErrorCopy(20022) || '分数需在 1~5 之间'
    return
  }
  ratingSubmitting.value = true
  ratingError.value = null
  try {
    const saved = await submitFeedback({
      log_id: logId,
      trace_id: traceId,
      score: ratingScore.value,
      comment: ratingComment.value.trim() || undefined,
    })
    ratingSaved.value = saved.score
  } catch (error) {
    // 三种失败各说各的：越界 / 归属不符 / 写入失败，下一步完全不同。
    ratingError.value =
      error instanceof ApiError
        ? feedbackErrorCopy(error.code) || errorText(error)
        : errorText(error)
  } finally {
    ratingSubmitting.value = false
  }
}

// —— ⑧ 语义旁证（#109） ——
/**
 * 触发一次旁证跑批并轮询状态。
 *
 * 三条约束来自后端契约（`app/api/routes/console_eval.py`）：
 * 1. 触发**只投递任务**、立刻回 `run_id`——跑批是分钟级的，挂在请求上必然超时；
 * 2. `queued` 是正常状态（任务还在队列里），不是「没有这次跑批」；
 * 3. `partial` 也是终态：裁判配额/超时会让部分题没评上，那**不是**检索退化，
 *    所以界面上不把它读成「失败」，只是如实显示已评分/未评分数。
 */
const sideEvidenceRunId = ref<string | null>(null)
const sideEvidenceStatus = ref<string>('')
const sideEvidenceJudgeModel = ref<string | null>(null)
const sideEvidenceSummary = ref<{
  context_precision?: number | null
  context_recall?: number | null
  count?: number
  scored?: number
  unscored?: number
} | null>(null)
/** 触发/轮询这条路自己出的错（网络、权限、任务投递失败）——「没跑起来」。 */
const sideEvidenceError = ref<string | null>(null)
/** 跑批本身跑完但判了 failed/partial 时，``status.json`` 里写的那个 ``error``——「跑了但没跑好」。 */
const sideEvidenceRunError = ref<string | null>(null)
const sideEvidenceStarting = ref(false)
let sideEvidenceTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 「投递出去多久了」——只为了说出那句「队列里没人消费」。
 *
 * 为什么需要它：触发成功只代表**任务进了队列**，不代表有人跑。只起 API 不起 Worker 时，
 * 状态会永远停在 `queued`，而界面上「正在排队」和「根本没人跑」长得一模一样。前端无法
 * 直接问「有没有 Worker」，但可以按时间说：`queued` 超过 45 秒，基本就不是排队了。
 */
const sideEvidenceQueuedSince = ref<number | null>(null)
const sideEvidenceNow = ref(Date.now())
let sideEvidenceClock: ReturnType<typeof setInterval> | null = null

const SIDE_EVIDENCE_QUEUE_HINT_SECONDS = 45

const sideEvidenceQueuedFor = computed(() => {
  if (sideEvidenceStatus.value !== 'queued' || sideEvidenceQueuedSince.value === null) return 0
  return Math.max(0, Math.round((sideEvidenceNow.value - sideEvidenceQueuedSince.value) / 1000))
})

const sideEvidenceQueueHint = computed(() => {
  if (sideEvidenceQueuedFor.value < SIDE_EVIDENCE_QUEUE_HINT_SECONDS) return ''
  return `已排队 ${sideEvidenceQueuedFor.value} 秒还没开始跑：多半是队列里没有 Worker 在消费（检查 worker 容器 / celery 进程）。`
})

/** 历史跑批：刷新页面后仍能看到「上次那轮」的结果，而不是只剩一片空白。 */
const sideEvidenceRuns = ref<SideEvidenceRun[]>([])
const sideEvidenceRunsError = ref<string | null>(null)

async function loadSideEvidenceRuns(): Promise<void> {
  try {
    sideEvidenceRuns.value = (await fetchSideEvidenceRuns(10)).items
    sideEvidenceRunsError.value = null
  } catch (error) {
    sideEvidenceRunsError.value = errorText(error)
  }
}

/** 报告弹窗：内容由后端按 `run_id` 读出来，前端不拼文件路径。 */
const reportRunId = ref<string | null>(null)
const reportText = ref<string | null>(null)
const reportLoading = ref(false)
const reportError = ref<string | null>(null)

async function openReport(runId: string): Promise<void> {
  reportRunId.value = runId
  reportText.value = null
  reportError.value = null
  reportLoading.value = true
  try {
    const payload = await fetchSideEvidenceReport(runId)
    reportText.value = payload.report
  } catch (error) {
    reportError.value = errorText(error)
  } finally {
    reportLoading.value = false
  }
}

function closeReport(): void {
  reportRunId.value = null
  reportText.value = null
  reportError.value = null
}

const SIDE_EVIDENCE_TERMINAL = new Set(['ok', 'partial', 'failed'])

function stopSideEvidencePolling(): void {
  if (sideEvidenceTimer !== null) {
    clearTimeout(sideEvidenceTimer)
    sideEvidenceTimer = null
  }
}

async function pollSideEvidence(runId: string): Promise<void> {
  try {
    const state = await fetchSideEvidence(runId)
    if (sideEvidenceRunId.value !== runId) return
    sideEvidenceStatus.value = state.status
    sideEvidenceJudgeModel.value = state.judge_model ?? null
    sideEvidenceSummary.value = state.summary ?? null
    sideEvidenceRunError.value = state.error ?? null
    if (SIDE_EVIDENCE_TERMINAL.has(state.status)) {
      stopSideEvidencePolling()
      // 跑完就刷一次历史：刚这轮应当立刻出现在列表里，不需要用户手动刷新。
      void loadSideEvidenceRuns()
      return
    }
  } catch (error) {
    sideEvidenceError.value = errorText(error)
    stopSideEvidencePolling()
    return
  }
  // 3 秒一轮：跑批本身是分钟级，再密只是白打接口；页面离开时由 onUnmounted 收尾。
  sideEvidenceTimer = setTimeout(() => void pollSideEvidence(runId), 3000)
}

async function startSideEvidence(): Promise<void> {
  if (sideEvidenceStarting.value) return
  sideEvidenceStarting.value = true
  sideEvidenceError.value = null
  sideEvidenceRunError.value = null
  stopSideEvidencePolling()
  try {
    const started = await triggerSideEvidence()
    sideEvidenceRunId.value = started.run_id
    sideEvidenceStatus.value = started.status
    sideEvidenceSummary.value = null
    sideEvidenceJudgeModel.value = null
    // 记下投递时刻：`queued` 停留过久时要能说出「队列里没人消费」。
    sideEvidenceQueuedSince.value = Date.now()
    sideEvidenceNow.value = Date.now()
    void pollSideEvidence(started.run_id)
  } catch (error) {
    sideEvidenceError.value = errorText(error)
  } finally {
    sideEvidenceStarting.value = false
  }
}

onMounted(() => {
  void loadSideEvidenceRuns()
  // 每秒推进一次「已排队多久」：只在 queued 时才有人读它，但计时器本身开销可以忽略，
  // 比「按需启停」少一条要维护的状态。
  sideEvidenceClock = setInterval(() => {
    sideEvidenceNow.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  stopSideEvidencePolling()
  if (sideEvidenceClock !== null) {
    clearInterval(sideEvidenceClock)
    sideEvidenceClock = null
  }
})
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
      <!--
        服务端在「开发身份模式」时，控制台签发的 Key **一把都不生效**（只认内置占位 Key）。
        这件事必须在这里说，而不是等检索失败后再说：失败时看到的「Key 无效」会把人送去
        反复重签一把永远不会生效的 Key。真实踩过。
      -->
      <div
        v-if="authDisabled"
        class="rounded-control border border-[#E8D9AE] bg-[#F7E9C9] px-3 py-2 text-[11px] leading-relaxed text-[#6E4E12]"
      >
        <strong>服务端当前是开发身份模式（AUTH_ENABLED=false）</strong>：检索会用内置开发身份，
        下面选的 Key <strong>不参与鉴权</strong>，所以拿真实 Key 直接调接口会得到 20010。
        要用真实 Key，把 <code class="font-mono">AUTH_ENABLED</code> 设为
        <code class="font-mono">true</code> 并重启 API。
      </div>
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

      <!--
        六段耗时（#103）：与运行摘要并列的第二个入口，同样是「需要时才展开」。
        它按 log_id 去控制面读回六段耗时——关掉观测也能从本地记录看到，
        没开 / 没记上时这里给明确文案，不留空白。
      -->
      <div class="mt-3 border-t border-line pt-3">
        <button
          type="button"
          class="flex items-center gap-1.5 text-[11px] text-muted transition hover:text-ink"
          @click="waterfallOpen = !waterfallOpen"
        >
          <span class="text-[9px]">{{ waterfallOpen ? '▼' : '▶' }}</span>
          六段耗时 · 按 log_id 看六段瀑布
        </button>

        <div v-if="waterfallOpen" class="mt-3">
          <!-- 有这次检索的响应时：一键看，不用抄 UUID（与评分控件共用同一个反查结果）。 -->
          <template v-if="ratingLogId !== null">
            <p class="text-[11px] leading-relaxed text-muted">
              看
              <span class="font-mono text-[10px] text-slate-500">{{ ratingLogId.slice(0, 8) }}…</span>
              这次检索的六段耗时（关掉观测也能从本地记录看）。
            </p>
            <div class="mt-2">
              <el-button size="small" :loading="waterfallLoading" @click="loadWaterfall">
                看这次检索的瀑布
              </el-button>
            </div>
          </template>
          <!-- 没有响应（例如只在工单里拿到一个 log_id）时，才退回手填。 -->
          <template v-else>
            <p class="text-[11px] leading-relaxed text-muted">
              手填
              <code class="rounded bg-slate-100 px-1 font-mono text-[10px] text-slate-500">log_id</code>
              （归属索引里那一行的 id）。这次的检索记录还没定位到，所以没有可一键查看的目标。
            </p>
            <div class="mt-2 flex flex-wrap items-center gap-2">
              <el-input
                v-model="logIdInput"
                size="small"
                class="max-w-sm"
                clearable
                placeholder="log_id（36 位 UUID）"
                @keyup.enter="loadWaterfall"
              />
              <el-button
                size="small"
                :loading="waterfallLoading"
                :disabled="trimmedLogId === ''"
                @click="loadWaterfall"
              >
                看瀑布
              </el-button>
            </div>
          </template>

          <p
            v-if="waterfallError !== null"
            class="mt-2 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700"
          >
            {{ waterfallError }}
          </p>
          <p v-else-if="ratingLogId === null && trimmedLogId === ''" class="mt-2 text-[11px] text-muted">
            还没填 log_id。填上之后再点「看瀑布」。
          </p>
          <StageWaterfall v-else-if="waterfall !== null" class="mt-3" :data="waterfall" />
        </div>
      </div>

      <!--
        评分（#106）：与运行摘要、瀑布图并列的第三个入口。
        它绑定的是**当前这次检索**（trace_id → 反查 log_id），不需要手填 UUID；
        观测开关不参与可用性判断——反馈以本地为准，观测栈挂了照样能打分。
      -->
      <div class="mt-3 border-t border-line pt-3">
        <p class="text-[11px] font-medium text-slate-600">给这次检索打分</p>

        <p v-if="ratingDisabledCopy !== ''" class="mt-2 text-[11px] leading-relaxed text-muted">
          {{ ratingDisabledCopy }}
        </p>
        <div v-else class="mt-2">
          <div class="flex flex-wrap items-center gap-1">
            <button
              v-for="score in [1, 2, 3, 4, 5]"
              :key="score"
              type="button"
              class="rounded-control border px-2 py-1 text-[13px] leading-none transition"
              :class="
                ratingScore === score
                  ? 'border-brand bg-brand/10 text-brand'
                  : 'border-line bg-white text-slate-400 hover:text-[#B7791F]'
              "
              :aria-label="`打 ${score} 分`"
              @click="ratingScore = score"
            >
              {{ scoreStars(score)[score - 1] }}
            </button>
            <span class="ml-2 text-[11px] text-muted">
              {{ ratingScore === 0 ? '还没选分数' : `${ratingScore} 分` }}
            </span>
          </div>
          <el-input
            v-model="ratingComment"
            class="mt-2 max-w-sm"
            size="small"
            maxlength="512"
            show-word-limit
            placeholder="一句话说明（可不填）"
          />
          <div class="mt-2 flex flex-wrap items-center gap-2">
            <el-button
              size="small"
              type="primary"
              :disabled="ratingScore === 0 || ratingSubmitting"
              :loading="ratingSubmitting"
              @click="sendRating"
            >
              提交评分
            </el-button>
            <span v-if="ratingSaved !== null" class="text-[11px] text-emerald-700">
              已评 {{ ratingSaved }} 分（再提交一次可改分）
            </span>
          </div>
          <p
            v-if="ratingError !== null"
            class="mt-2 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700"
          >
            {{ ratingError }}
          </p>
        </div>
      </div>
    </el-card>

    <!--
      ⑤ 语义旁证（#109）：一个触发入口 + 状态轮询。
      它**不进闸门**——门禁是确定性那一套（零 LLM、可复现），这里只是「阶段性体检」，
      所以文案里不说「通过 / 不通过」，只说数字与裁判模型。跑批是分钟级的，触发只投递
      任务，结论靠轮询 status.json。
    -->
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">语义旁证</h2>
            <p class="mt-1 text-xs text-slate-500">
              用 LLM 裁判跑固定 50 题，出 CP / CR。只提示、不阻断；门禁另有确定性口径。
            </p>
          </div>
          <el-button
            size="small"
            :loading="sideEvidenceStarting"
            :disabled="sideEvidenceStatus === 'running' || sideEvidenceStatus === 'queued'"
            @click="startSideEvidence"
          >
            触发一次旁证
          </el-button>
        </div>
      </template>

      <p v-if="sideEvidenceError !== null" class="rounded border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700">
        {{ sideEvidenceError }}
      </p>
      <p v-else-if="sideEvidenceRunId === null" class="text-sm text-slate-400">
        还没触发过。点右上角「触发一次旁证」跑一轮（约几分钟）。
      </p>
      <div v-else class="space-y-1.5 text-[11px]">
        <p>
          <span class="text-muted">run</span>
          <span class="ml-1 font-mono text-slate-500">{{ sideEvidenceRunId }}</span>
          <span class="ml-2 text-muted">状态</span>
          <span class="ml-1 font-medium">{{ sideEvidenceStatus }}</span>
          <el-button
            v-if="SIDE_EVIDENCE_TERMINAL.has(sideEvidenceStatus)"
            link
            size="small"
            class="ml-2"
            @click="openReport(sideEvidenceRunId!)"
          >
            看报告
          </el-button>
        </p>
        <p v-if="sideEvidenceJudgeModel" class="text-muted">
          裁判模型 {{ sideEvidenceJudgeModel }}（换模型 = 换尺子，与旧报告不可比）
        </p>
        <p v-if="sideEvidenceSummary" class="font-mono text-slate-600">
          CP {{ formatPercent(sideEvidenceSummary.context_precision) }} ·
          CR {{ formatPercent(sideEvidenceSummary.context_recall) }} ·
          已评分 {{ sideEvidenceSummary.scored }} / 未评分 {{ sideEvidenceSummary.unscored }}
        </p>
        <!--
          样本量必须和 CP/CR 一起看：分母是「真正评上的题数」，不是「跑了几道题」。
          把 n=5 读成「50 题的平均」是这套旁证最容易被误读的地方。
        -->
        <p v-if="sideEvidenceSummary?.scored !== undefined" class="text-muted">
          上面两个数的分母是「已评分」那一列，不是题目总数。
        </p>
        <p v-if="sideEvidenceRunError" class="text-[#6E4E12]">{{ sideEvidenceRunError }}</p>
        <p v-if="sideEvidenceQueueHint" class="text-[#B45309]">{{ sideEvidenceQueueHint }}</p>
      </div>

      <!-- 历史：刷新页面后仍能看到上一轮，而不是只剩空白 -->
      <div class="mt-3 border-t border-line pt-2">
        <div class="flex items-center justify-between">
          <p class="text-[11px] font-medium text-slate-600">最近跑过的几轮</p>
          <el-button link size="small" @click="loadSideEvidenceRuns">刷新</el-button>
        </div>
        <p v-if="sideEvidenceRunsError !== null" class="mt-1 text-[11px] text-red-700">
          {{ sideEvidenceRunsError }}
        </p>
        <p v-else-if="sideEvidenceRuns.length === 0" class="mt-1 text-[11px] text-muted">
          还没有跑批记录。
        </p>
        <ul v-else class="mt-1 space-y-1">
          <li
            v-for="run in sideEvidenceRuns"
            :key="run.run_id"
            class="flex flex-wrap items-baseline gap-2 text-[11px]"
          >
            <span class="font-mono text-slate-500">{{ run.run_id }}</span>
            <span class="font-medium">{{ run.status }}</span>
            <span v-if="run.summary" class="font-mono text-slate-600">
              CP {{ formatPercent(run.summary.context_precision) }} ·
              CR {{ formatPercent(run.summary.context_recall) }} ·
              {{ run.summary.scored }}/{{ run.summary.unscored }}
            </span>
            <el-button v-if="run.has_report" link size="small" @click="openReport(run.run_id)">
              看报告
            </el-button>
          </li>
        </ul>
      </div>
    </el-card>

    <!-- 报告弹窗：Markdown 原文，用等宽字体原样显示（不引 markdown 渲染器，少一份依赖） -->
    <el-dialog
      :model-value="reportRunId !== null"
      title="旁证报告"
      width="820px"
      @update:model-value="closeReport"
    >
      <p v-if="reportRunId !== null" class="mb-2 font-mono text-[11px] text-slate-500">
        run {{ reportRunId }}
      </p>
      <p v-if="reportError !== null" class="text-[11px] text-red-700">{{ reportError }}</p>
      <p v-else-if="reportLoading" class="text-[11px] text-muted">读取中…</p>
      <p v-else-if="reportText === null" class="text-[11px] text-muted">
        这一轮没有报告文件（跑批可能还在排队，或中途失败没落地）。
      </p>
      <pre
        v-else
        class="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-control bg-slate-50 p-3 text-[11px] leading-relaxed text-slate-700"
      >{{ reportText }}</pre>
    </el-dialog>

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
