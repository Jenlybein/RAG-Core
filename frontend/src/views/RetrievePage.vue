<script setup lang="ts">
/**
 * 检索页：应用 Key → 勾库 → 发检索 → 结果卡片 + 只读运行摘要。
 *
 * 五条不许越过的线（issue #87 / #92 / spec #82）：
 * 1. 请求体只有 `kb_ids` + `query`：本页没有任何档位按钮或高级参数输入，
 *    `profile` / `top_k` / `mode` / `rerank` / `rewrite` 一律不进请求体；
 * 2. 运行摘要只读响应 `metadata`，**不看页面输入**——摘要回答的是「后端这次实际
 *    生效了什么」，拿页面配置冒充它就等于把调试台变成许愿台；
 * 3. 身份只有用户在**主面板常驻输入框**里粘贴、点「应用」的那把 Key：本页是全仓库唯一
 *    的数据面入口的使用者（`ragService.retrieve()`），未应用 Key 时请求层会抛可操作的错误；
 * 4. 可勾选的库由**服务端**决定：应用 Key 后先调 `GET /api/v1/knowledge-bases`（Bearer 这把
 *    Key），拿回来的就是这把 Key 真正能查的库。不再用控制面的库树 + 前端筛白名单——
 *    那是客户端算的答案，与服务端判定漂了会显示一个并不被认可的集合（详见
 *    `services/scopeService.ts`）；
 * 5. 本页**不拉** `GET /console/keys`：列表里没有原文，而「这把 Key 有没有效」只有拿它
 *    真的打一次数据面请求才作数。拉一份列表回来再本地比对，会把「服务端还没说话」
 *    误当成「Key 不对」。
 *
 * 结果卡片只展示契约里有的字段；`latency_ms` / `log_id` 后端没有（00-facts §5 明确列过），
 * 这里也不编造。`metadata.trace_id` 从 #101 起有了，但本页不摊它——它在这张卡上的用途只有
 * 一个：与瀑布图里那根线对上（见下方「六段耗时」面板的说明）。
 */
import { useMutation, useQuery } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import CollapsibleCard from '../components/CollapsibleCard.vue'
import ComparePanel from '../components/ComparePanel.vue'
import DataPlanePanel from '../components/DataPlanePanel.vue'
import PageHeader from '../components/PageHeader.vue'
import ResponseFields from '../components/ResponseFields.vue'
import RunSummary from '../components/RunSummary.vue'
import StageWaterfall from '../components/StageWaterfall.vue'
import { readRetrieveApiKey, writeRetrieveApiKey } from '../lib/apiKeySession'
import { keyIdOf } from '../lib/apiKey'
import { ApiError } from '../lib/envelope'
import { fetchWaterfall, resolveLogId } from '../services/consoleTraces'
import { submitFeedback } from '../services/feedbackService'
import { decomposeRetrieve, research, retrieve, rewriteRetrieve } from '../services/ragService'
import {
  MY_KB_QUERY_KEY,
  fetchMyKnowledgeBases,
  type MyKnowledgeBase,
} from '../services/scopeService'
import { fetchRuntimeInfo, runtimeInfoQueryKey } from '../services/traceService'
import { useRetrieveStore } from '../stores/retrieve'
import type { ResearchData } from '../types/research'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'
import type { RetrievalWaterfall } from '../types/traceWaterfall'
import { feedbackErrorCopy, isValidScore, scoreStars } from '../utils/feedback'
import { DATA_PLANE_ENDPOINTS } from '../utils/dataPlaneEndpoints'
import { pushHistory, type CompareHistoryEntry } from '../utils/retrieveCompare'
import { selectedKbSummary } from '../utils/retrieveSelection'
import {
  describeEmptyReason,
  formatPercent,
  formatRank,
  formatScore,
} from '../utils/format'

const route = useRoute()
const router = useRouter()
const store = useRetrieveStore()

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    // code=-1 是协议/连通性问题，没有业务码可报；其余把码带上，方便对照错误码表。
    return error.code === -1 ? error.message : `${error.message}（业务码 ${error.code}）`
  }
  return error instanceof Error ? error.message : String(error)
}

/**
 * 「后端原话」优先的错误文案。
 *
 * 应用 Key 失败时要摊在输入框下面的是**服务端说了什么**，不是前端按错误码表猜的一句话：
 * 数据面 20010 有四种成因（缺 token / Key 不存在 / Key 停用 / 服务端还在开发身份模式），
 * 只有后端那句话能分清是哪一种。后端没给原文（纯 HTTP 失败、连不上）时才退回通用文案。
 */
function backendErrorText(error: unknown): string {
  if (error instanceof ApiError && error.backendMsg !== '') return error.backendMsg
  return errorText(error)
}


// —— ① 应用 Key 与检索范围 ——

/** 上一次会话留在 `sessionStorage` 里的 Key：有就预填，刷新不必重粘。 */
const initialStoredKey = readRetrieveApiKey()
const keyInput = ref(initialStoredKey ?? '')
/** 原文已经写进请求层（`lib/activeKey`）。它不是「服务端认可」的同义词，后者看库清单。 */
const keyApplied = ref(false)
/** 上一次成功应用的原值：用来判断这次是否算「换了 Key」，决定要不要提示「请重新选库」。 */
const lastAppliedValue = ref<string | null>(null)
/** 每次应用 +1：查询键跟着变，同一把 Key 再点一次「应用」也会重新问一遍服务端。 */
const applySerial = ref(0)

/**
 * 应用（或更换）Key。
 *
 * 顺序是刻意的：**先清空已选库，再去拉库清单**。留着旧选择会让下一次检索撞 20008，
 * 而那个错误要等请求之后才出现；新 Key 能查什么只有服务端知道，所以「先清空、再问」
 * 是唯一不会显示出一个过期集合的顺序。
 *
 * 原文同时写进 `sessionStorage`：刷新后调试台还认得这把 Key。它只活在这次浏览器会话，
 * 换一个会话就没了——长期凭据不该因为「上次来过」在另一天自动复活。
 */
function applyKey(options: { silent?: boolean } = {}): void {
  const raw = keyInput.value.trim()
  if (raw === '') {
    ElMessage.warning('先把 Key 粘进来')
    return
  }
  const changed = lastAppliedValue.value !== raw
  const hadSelection = store.selectedCount > 0

  // `selectKey` 一次写完「请求层认哪把」与「页面选哪把」，并顺手按新白名单裁掉已选库；
  // 这里给空白名单 = 全部裁空（服务端清单回来之前，没有任何库可以被认为是可查的）。
  store.selectKey({ keyId: keyIdOf(raw), keyValue: raw, allowedKbIds: [] })
  writeRetrieveApiKey(raw)
  lastAppliedValue.value = raw
  keyApplied.value = true
  applySerial.value += 1

  if (options.silent !== true && (changed || hadSelection)) {
    ElMessage.info('Key 已更换，请重新选库')
  }
}

// 挂载时若上次留过 Key：预填并自动应用一次（静默，不弹「请重新选库」）。
// 放在预填 `?kb_id=` 之前：应用会清空选择，深链预填必须排在这次清空之后才不会白丢。
if (initialStoredKey !== null) applyKey({ silent: true })

/**
 * `?kb_id=` 预填。同样的坑上传页已经踩过：同一路由只改 query 不会重新挂载组件，
 * 只读一次会让勾选停在上一个库，所以初次读一次、之后再 watch。
 * 预填可能落在「这把 Key 查不到」的库里；那是正常状态，服务端清单回来后会再裁一次。
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

/**
 * 「这把 Key 能查哪些库」——**问服务端**，不在前端自己筛。
 *
 * 原来这里拉的是控制面的库树（会话身份、不做范围收窄），再用 `/console/keys` 里的
 * `allowed_kb_ids` 在前端把白名单外的置灰。那是客户端算的答案：配置一改、库一删、
 * 或者 `unrestricted_scope` 之类生效，界面就会显示一个服务端并不认可的集合，而错误
 * 要等检索撞 20008 才出现。
 *
 * 现在这条 `GET /api/v1/knowledge-bases`（Bearer 这把 Key）返回的就是服务端按同一份
 * policy 过滤后的结果，与检索用的是同一个判定。链路上服务端是唯一真相。
 *
 * 没有应用 Key 时不发请求（`enabled`）：那时请求头里没有可用的 Key，发出去只会换一个
 * 看起来像「Key 失效」的 20010，而实际只是「还没应用」。
 *
 * 查询键里带 `applySerial` 而不是只带 Key 的 id：一个是同一把 Key 可能被重新应用
 * （用户想再问一次），另一个是格式不对的串解不出 id（`null`）——两种情况下只按 id
 * 区分就会命中旧缓存，界面会停在上一次的结果上。
 */
const myKbsQuery = useQuery({
  queryKey: computed(() => [...MY_KB_QUERY_KEY, store.selectedKeyId ?? '', applySerial.value]),
  queryFn: fetchMyKnowledgeBases,
  enabled: computed(() => keyApplied.value),
  retry: false,
})
const myKbs = computed<MyKnowledgeBase[]>(
  () => myKbsQuery.data.value?.knowledge_bases ?? [],
)
const kbs = computed<MyKnowledgeBase[]>(() => myKbs.value)
/**
 * 三个状态都显式标 `computed<boolean>`：它们是**布尔**，不是 vue-query 的 ref。
 *
 * 这里踩过一次：写成 `store.selectedKeyId !== null && myKbsQuery.isPending`（漏了
 * `.value`）时，`&&` 返回的是那个 **ref 对象**，永远为真——于是请求明明成功（日志里
 * 有 200），界面却永远停在「正在问服务端…」。TypeScript 也拦不住：`boolean | Ref`
 * 对模板里的真值判断完全合法。标上 `computed<boolean>` 之后，同样的笔误会是类型错误。
 */
const treePending = computed<boolean>(() => keyApplied.value && myKbsQuery.isPending.value)
const treeLoaded = computed<boolean>(() => myKbsQuery.isSuccess.value)
/** 应用后拉库清单失败：输入框下面摊的就是这段，而且「选择知识库」要跟着置灰。 */
const keyApplyError = computed<string>(() => {
  const error = myKbsQuery.error.value
  return error === null ? '' : backendErrorText(error)
})
/** 应用成功（服务端认可这把 Key，库清单回来了）。 */
const keyVerified = computed<boolean>(() => keyApplied.value && myKbsQuery.isSuccess.value)
/** 正在应用 / 验证。 */
const keyVerifying = computed<boolean>(() => keyApplied.value && myKbsQuery.isPending.value)
/** 选库入口的可用性只看一件事：这把 Key 能不能列出库。列不出来就不让选。 */
const pickerDisabled = computed<boolean>(() => !keyVerified.value)
/**
 * 「当前可勾的库」= 服务端刚返回的那一份。
 *
 * 注意它**不是**配置里的白名单：那是配置的**意图**；服务端返回的是同一份 policy 应用到
 * 真实库表之后的**结果**（库可能被删、可能被租户过滤）。能否勾选看结果，不看意图。
 */
const availableKbIds = computed(() => new Set(kbs.value.map((kb) => kb.kb_id)))

/** 接口清单里 `GET /documents` 的试跑用「当前勾选的第一个库」——没勾就没有按钮。 */
const firstSelectedKbId = computed<string | null>(() => store.selectedKbIds[0] ?? null)
/** 可试跑的只读接口数：标题右侧那行文案用它，省得两个数字各自漂。 */
const trialableCount = computed(
  () => DATA_PLANE_ENDPOINTS.filter((endpoint) => endpoint.trial !== undefined).length,
)

/**
 * 「选择知识库」弹窗的草稿：取消 = 不改动主面板上的选择。
 *
 * 为什么不直接把勾选框绑到 store（原来的做法）：底部按钮既然是「取消 / 确定」，
 * 取消就必须真的什么也没发生。绑 store 的话，勾一下立刻生效，「取消」只是一句谎话。
 */
const dialogSelection = ref<string[]>([])

function openKbDialog(): void {
  if (pickerDisabled.value) return
  // 打开时拷一份当前选择：改勾选只动草稿，点「确定」才落到 store。
  dialogSelection.value = [...store.selectedKbIds]
  configDialogOpen.value = true
}

function confirmKbDialog(): void {
  store.setSelection(dialogSelection.value)
  configDialogOpen.value = false
}

function dropUnpickable(): void {
  dialogSelection.value = dialogSelection.value.filter((kbId) => availableKbIds.value.has(kbId))
}

/**
 * 服务端清单回来之后**再裁一次**选择——这一次用的是真答案。
 *
 * 第一轮裁剪是应用 Key 那一刻的「全部清空」，因为那时网络还没回来；服务端返回的是
 * 「意图应用到真实库表之后的结果」（库可能被删、可能被租户过滤）。两轮都走同一个
 * `store.resyncScope`，所以不会出现两套裁剪逻辑。
 *
 * 只在 `isSuccess` 时裁：请求还在飞时 `kbs` 是空数组，拿它当「一个库都没有」会把用户
 * 的选择清空——那是把「还没问到」当成「没有」。
 */
watch(
  [myKbs, () => myKbsQuery.isSuccess.value],
  ([rows, loaded]) => {
    if (!loaded) return
    const removed = store.resyncScope(rows.map((kb) => kb.kb_id))
    if (removed.length > 0) {
      ElMessage.warning(`这些库已不在这把 Key 的可用范围内，已移出选择：${removed.join('、')}`)
    }
  },
)



/**
 * 已选但这一次勾不上的库（例如 URL 预填了一个不在这把 Key 可用范围里的旧库 ID）。
 *
 * 不静默删掉它们：用户可能正想查一个不在这把 Key 范围里的库；但必须提前说明检索
 * 大概率会失败，并给一个「移除」的动作。判断依据是草稿，因为它决定用户接下来会保存
 * 什么——拿主面板上的旧选择提示，会提示一件点「确定」之后才会发生的事。
 */
const unpickableSelected = computed(() =>
  dialogSelection.value.filter((kbId) => !availableKbIds.value.has(kbId)),
)

/** el-checkbox-group 的事件载荷是联合类型；本页的选项值全是字符串，这里只做类型收窄。 */
function onSelectionChange(values: Array<string | number | boolean>): void {
  dialogSelection.value = values.filter((value): value is string => typeof value === 'string')
}

// —— ② 用哪一项能力（四条端点）与问题 ——

/**
 * 四条数据面端点就是四项**能力**，各有各的账：`retrieve` 零模型调用、`rewrite` /
 * `decompose` 各一次模型调用 + N 次内核、`research` 多轮补证。所以「用哪条」是发起
 * 检索前的一个决定，而不是页面的固定行为——调试台要能换着比。
 *
 * 能不能选**不再由前端置灰**：本页不拉 `/console/keys`，手里没有这把 Key 的授权位。
 * 猜一个「没开这个能力」并灰掉按钮，就是把「不知道」显示成「不行」；真实结论只有
 * 服务端能回——没开这个能力时是 20013，报错文案会直接说清是哪一个。
 */
type Capability = 'retrieve' | 'rewrite' | 'decompose' | 'research'

interface CapabilityOption {
  key: Capability
  label: string
  /** 一句话说清这条链路花什么，用来选。 */
  hint: string
}

const CAPABILITY_OPTIONS: readonly CapabilityOption[] = [
  { key: 'retrieve', label: '检索', hint: '原句直查，零模型调用' },
  { key: 'rewrite', label: '改写后检索', hint: '一次改写，原句与改写句并池' },
  { key: 'decompose', label: '分解后检索', hint: '拆子问题，各查一次再合并' },
  { key: 'research', label: '研究模式', hint: '多轮补证 + 引用块，最贵' },
]

const runCapability = ref<Capability>('retrieve')

/** 这次响应用的是哪条链路：渲染分支与「摘要能不能用」都读它，不读页面当前选择。 */
const lastCapability = ref<Capability>('retrieve')

function callCapability(
  capability: Capability,
  payload: RetrieveRequest,
): Promise<RetrieveData | ResearchData> {
  switch (capability) {
    case 'rewrite':
      return rewriteRetrieve(payload)
    case 'decompose':
      return decomposeRetrieve(payload)
    case 'research':
      return research(payload)
    default:
      return retrieve(payload)
  }
}


const queryText = ref('')

const trimmedQuery = computed(() => queryText.value.trim())
/**
 * 按钮禁用时把原因说出来：只灰掉按钮不说原因，用户会以为自己点错了。
 *
 * 能力授权位不在前端判定（本页不拉 Key 列表，也就看不到它）；这个按钮只需要回答
 * 「现在为什么点不了」——没应用 Key / Key 还在验证 / Key 被拒 / 没选库 / 没问题。
 * 授权不足会由服务端在检索时给出 20013 与后端原文，那是唯一作数的答案。
 */
const blockReason = computed(() => {
  if (!keyApplied.value) return '还没应用 Key —— 在上面粘贴 API Key 后点「应用」'
  if (keyVerifying.value) return '正在验证这把 Key 能查哪些库…'
  if (keyApplyError.value !== '') return 'Key 不可用 —— 看输入框下面那句报错'
  if (store.selectedCount === 0) return '还没选库 —— 点「选择知识库」勾选至少一个知识库'
  if (trimmedQuery.value === '') return '先写要检索的问题（全空白不算问题）'
  return ''
})

// —— ③ 结果 / ④ 摘要 ——
const result = ref<RetrieveData | null>(null)
/**
 * `/rag/research` 的响应**另存一格**，不并进 `result`：它是另一个形状（`answer` /
 * `citations` / `materials` / `plan` / `rounds` / `coverage`），合并成一个「都有」的类型
 * 会让两边的字段都变成可能为空，渲染反而更容易错。
 */
const researchResult = ref<ResearchData | null>(null)
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

interface RunVars {
  capability: Capability
  payload: RetrieveRequest
}

const { mutate, isPending: running } = useMutation<
  RetrieveData | ResearchData,
  Error,
  RunVars
>({
  mutationFn: (vars: RunVars) => callCapability(vars.capability, vars.payload),
  onMutate: () => {
    runError.value = null
  },
  onSuccess: (data, vars) => {
    // 页面上的选择此刻可能已经变了，所以渲染分支读**这次请求**带的那一项能力。
    lastCapability.value = vars.capability
    result.value = null
    researchResult.value = null

    if (vars.capability === 'research') {
      const researchData = data as ResearchData
      researchResult.value = researchData
      // Research 不进对比历史：`ComparePanel` 比的是同一份检索结果的排名与分数，
      // 而 Research 的产物是引用块 + 多轮材料，两列并排比不出「改了什么」。
      void prepareRating(researchData.metadata.trace_id)
      return
    }

    const retrieveData = data as RetrieveData
    result.value = retrieveData
    // 只有成功的检索才进历史：失败没有 `RetrieveData` 可对比，硬造一条空记录会让
    // 「第 N 次」名不副实，也会把基准的编号整体错位。
    const seq = nextSeq
    nextSeq += 1
    // 只存响应引用本身：`result` 与历史都只读这份数据，页面从不修改它，
    // 所以两份引用指向同一份快照是安全的（真去深拷贝一遍只会多一份可能被改的副本）。
    history.value = pushHistory(history.value, {
      seq,
      at: new Date().toISOString(),
      data: retrieveData,
    })
    // 这一次的线换掉了：重新反查 log_id，评分控件随之绑定到**这次**结果。
    // 反查失败不报错弹窗（那只是「不能打分」），由控件里的文案说明。
    void prepareRating(retrieveData.metadata.trace_id)
  },
  onError: (error) => {
    // 本次失败就清掉上一次的结果：旧卡片配新报错会让人以为「这次也成功了一部分」，
    // 而摘要更必须永远对应最近一次响应，不能停在上一次。
    result.value = null
    researchResult.value = null
    ratingLogId.value = null
    ratingSaved.value = null
    ratingScore.value = 0
    ratingError.value = null
    runError.value = {
      message: errorText(error),
      // 20013 在这一页有两种成因：库数超限，或者这套配置没开当前能力。提示要能同时解释。
      limitHint: error instanceof ApiError && error.code === 20013,
    }
  },
})

function submitRetrieve(): void {
  if (blockReason.value !== '' || running.value) return
  // 拷一份再发：请求在途时用户改勾选，不该顺手改掉这次请求的 `kb_ids`。
  mutate({
    capability: runCapability.value,
    payload: { kb_ids: [...store.selectedKbIds], query: trimmedQuery.value },
  })
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
 * 「选择知识库」弹窗。
 *
 * Key 不再进弹窗：它已经是主面板上常驻的输入框与「应用」按钮。弹窗只负责一件事——
 * 从服务端认可的那份库清单里挑要查的库，底部按钮「取消 / 确定」对应草稿与提交。
 */
const configDialogOpen = ref(false)


/**
 * 结果列表读**两种**响应里的同一件事：`/retrieve` 家族叫 `retrieved_chunks`，
 * `/research` 叫 `materials`——后端是同一个值对象（`RetrievedChunk`），所以这里只按
 * 「这次是哪条能力」取那一栏，不为两边各写一套渲染。
 */
const chunks = computed(() => {
  if (researchResult.value !== null) return researchResult.value.materials
  return result.value?.retrieved_chunks ?? []
})
const emptyReason = computed(() => result.value?.metadata.retrieval.empty_reason ?? null)

/**
 * 这次响应的**顶层字段全集**（列表那两栏除外）。
 *
 * 为什么把列表剔掉而不是整份 dump：`/rewrite` 一次能回 50 条切片，逐条字段摊在树里会
 * 把整棵树埋掉；而列表里的每一条本来就有自己的卡片，卡片下方还能再摊「全部字段」。
 * 于是「全部字段」这条要求落到两处、各司其职：
 * * 这里——顶层与 `metadata` 一整棵树（`rerank` / `query_optimization` /
 *   `decomposition` / `tenant_policy` / Research 的 `plan` / `rounds` / `coverage` …）；
 * * 每条结果卡片——那一条切片的每一个字段。
 *
 * 不按接口写死字段表：四个端点各带各的分组，写死的表天然会漏。
 */
const responseFields = computed<Record<string, unknown> | null>(() => {
  const data = researchResult.value ?? result.value
  if (data === null) return null
  const fields: Record<string, unknown> = {
    ...(data as unknown as Record<string, unknown>),
  }
  for (const key of ['retrieved_chunks', 'materials']) {
    const list = fields[key]
    if (Array.isArray(list)) {
      // 用一句话交代条数，把逐条字段明确指到卡片上——不静默丢掉那一栏。
      fields[key] = `［${list.length} 条，逐条字段见上方各卡片的「全部字段」］`
    }
  }
  return fields
})

const responseJson = computed<string>(() => {
  const data = researchResult.value ?? result.value
  return data === null ? '' : JSON.stringify(data, null, 2)
})

async function copyResponseJson(): Promise<void> {
  if (responseJson.value === '') return
  try {
    await navigator.clipboard.writeText(responseJson.value)
    ElMessage.success('已复制完整响应的 JSON')
  } catch {
    ElMessage.error('浏览器没有授予剪贴板权限，没能复制')
  }
}

/**
 * 每条结果自己的「全部字段」展开状态（按 chunk_id 记）。
 *
 * 默认收起：50 条切片各摊 20 个字段会把正文埋掉。但**要能摊开**——`source_chunk_ids` /
 * `section_id` / `rrf_score` 这些字段没有卡片展示，而它们恰恰是排障时要看的。
 */
const expandedChunks = ref<Set<string>>(new Set())

/**
 * 「全部展开 / 全部收起」的广播：`null` = 还没点过（各节点按默认深度显示）。
 * 点过之后是 `'open' / 'closed'`，序号一变，树里每一层都对齐到这个方向。
 */
const expandAll = ref<'open' | 'closed' | null>(null)
const expandSerial = ref(0)

function expandTree(open: boolean): void {
  expandAll.value = open ? 'open' : 'closed'
  expandSerial.value += 1
}

function toggleChunkFields(chunkId: string): void {
  // 换一个新 Set 而不是原地增删：Vue 才对 Set 的变更做响应（原地改它不会触发渲染）。
  const next = new Set(expandedChunks.value)
  if (next.has(chunkId)) next.delete(chunkId)
  else next.add(chunkId)
  expandedChunks.value = next
}
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
const currentTraceId = computed(
  () => researchResult.value?.metadata.trace_id ?? result.value?.metadata.trace_id ?? null,
)

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

</script>

<template>
  <section class="space-y-6">
  <PageHeader
    title="调试台"
  />
    <!-- ① 应用 Key 与检索范围 -->
    <!--
      Key 常驻在主面板，而不是收进弹窗：调试台的每一步都要身份，粘一把 Key 再开弹窗
      确认「现在用的是哪把」，等于把身份藏起来。输入框只负责写请求层；能不能用由服务端
      说了算——拉一次知识库列表就是那次验证。弹窗里于是只剩「选哪些库」。
    -->
    <el-card shadow="never">
      <template #header>
        <h2 class="text-sm font-semibold text-ink">发起检索</h2>
      </template>

      <!--
        服务端是「开发身份模式」时，控制台签发的 Key **一把都不生效**（只认内置占位 Key）。
        必须在检索前说，否则失败时看到的「Key 无效」会把人送去反复重签一把永远不会生效的 Key。
        一行说完，怎么改塞进 tooltip：这条提示常驻，句子越长越像背景噪音。
      -->
      <div
        v-if="authDisabled"
        class="mb-3 flex flex-wrap items-center gap-2 rounded-control border border-[#f0dfae] bg-[#fff8e6] px-3 py-2 text-xs leading-relaxed text-[#7a5b00]"
      >
        <span>
          当前是开发身份模式：这里填任何值都能看到全部知识库，检索不会按它的白名单或额度过滤。
        </span>
        <el-tooltip
          content="把 AUTH_ENABLED 设为 true 并重启 API，控制台签发的 Key 才会生效"
          placement="top"
          :show-after="200"
        >
          <span class="cursor-help underline decoration-dotted underline-offset-2">怎么改</span>
        </el-tooltip>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <el-input
          v-model="keyInput"
          type="password"
          show-password
          autocomplete="off"
          spellcheck="false"
          placeholder="粘贴 API Key（rc_live_…）"
          class="max-w-md font-mono"
          @keyup.enter="applyKey()"
        />
        <el-button :loading="keyVerifying" @click="applyKey()">应用</el-button>
        <span v-if="keyVerified" class="text-xs text-emerald-700">已应用</span>
        <button
          type="button"
          class="text-xs text-brand underline underline-offset-2 transition hover:text-ink"
          @click="router.push('/keys')"
        >
          去 API Key 页获取
        </button>
      </div>
      <!--
        失败时摊的是**后端原话**：数据面的「Key 无效」有四种成因，前端按错误码表猜一句
        会盖掉唯一能分清的那条线索；也不偷偷降级成「按公开库继续查」——身份不对就停。
      -->
      <p v-if="keyApplyError !== ''" class="mt-2 text-xs leading-relaxed text-red-700">
        {{ keyApplyError }}
      </p>

      <div class="mt-4 flex flex-wrap items-center gap-2">
        <el-button size="small" :disabled="pickerDisabled" @click="openKbDialog">
          选择知识库
        </el-button>
        <span class="text-xs text-muted">{{ selectedKbSummary(store.selectedCount) }}</span>
      </div>

      <div class="mt-5 border-t border-line pt-5">
        <!-- 用哪项能力：四条端点各有各的成本，选错就不是「同一次检索的另一种说法」。 -->
        <div class="mb-4">
          <label class="mb-1.5 block text-xs text-muted">用哪项能力</label>
          <el-radio-group v-model="runCapability" size="small">
            <el-tooltip
              v-for="option in CAPABILITY_OPTIONS"
              :key="option.key"
              :content="option.hint"
              placement="top"
              :show-after="200"
            >
              <span class="inline-flex">
                <el-radio-button :value="option.key">
                  {{ option.label }}
                </el-radio-button>
              </span>
            </el-tooltip>
          </el-radio-group>
        </div>
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
          <!-- 动作行右对齐：左端是「为什么现在点不了」，右端是这一卡唯一的动作。 -->
          <div class="flex flex-wrap items-center justify-end gap-2">
            <span v-if="blockReason !== ''" class="mr-auto text-xs text-muted">
              {{ blockReason }}
            </span>
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
      「选择知识库」弹窗：只放勾选。底部是「取消 / 确定」——勾选改的是草稿，
      确定才落到主面板上的选择；取消 = 什么也没发生。
    -->
    <el-dialog
      v-model="configDialogOpen"
      append-to-body
      title="选择知识库"
      width="880px"
    >
      <div class="space-y-4 text-sm">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <span class="text-xs font-medium text-ink">要查的知识库</span>
          <span class="text-xs text-muted">
            已选 {{ dialogSelection.length }} / 共 {{ kbs.length }}
          </span>
        </div>
        <p v-if="!keyApplied" class="text-xs text-muted">先在上面粘贴并应用一把 Key</p>
        <p v-else-if="treePending" class="text-xs text-muted">
          正在问服务端：这把 Key 能查哪些库…
        </p>
        <div
          v-else-if="keyApplyError !== ''"
          class="rounded-control border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
        >
          {{ keyApplyError }}
        </div>
        <div
          v-else-if="kbs.length === 0"
          class="rounded-control border border-line bg-surface-muted px-3 py-2 text-xs text-slate-600"
        >
          这把 Key 目前查不到任何库。
          <RouterLink to="/keys" class="text-brand underline">去 Key 管理页调整</RouterLink>
        </div>
        <el-checkbox-group
          v-else
          class="grid gap-2 sm:grid-cols-2"
          :model-value="dialogSelection"
          @update:model-value="onSelectionChange"
        >
          <template v-for="kb in kbs" :key="kb.kb_id">
            <!-- 可点的小卡而不是裸复选框：有边框才读得出「这是一个可以点的对象」。 -->
            <el-checkbox
              :value="kb.kb_id"
              class="kb-option !mr-0"
              :class="{ 'kb-option--on': dialogSelection.includes(kb.kb_id) }"
            >
              <span class="flex flex-col leading-tight">
                <span class="text-[13px] text-ink">{{ kb.name }}</span>
                <span class="mt-0.5 font-mono text-xs text-slate-400">{{ kb.kb_id }}</span>
              </span>
            </el-checkbox>
          </template>
        </el-checkbox-group>

        <div
          v-if="treeLoaded && unpickableSelected.length > 0"
          class="rounded-control border border-[#f0dfae] bg-[#fff8e6] px-3 py-2 text-xs leading-relaxed text-[#7a5b00]"
        >
          <p>这些已选库不在这把 Key 的可用范围内，带它们检索大概率会失败：</p>
          <p class="mt-1 text-slate-700">{{ unpickableSelected.join('、') }}</p>
          <el-button class="mt-2" size="small" @click="dropUnpickable">移除这些选择</el-button>
        </div>
      </div>

      <template #footer>
        <div class="flex justify-end gap-2">
          <el-button @click="configDialogOpen = false">取消</el-button>
          <el-button type="primary" @click="confirmKbDialog">确定</el-button>
        </div>
      </template>
    </el-dialog>

    <!--
      主体两栏：左边是「这一次检索的答案」，右边是「诊断」（旁证 / 接口清单）。
      窄屏自动落成单列。之前四张卡一路往下堆，一屏里既有正文又有仪表读数和接口表，
      眼睛没有落点；拆成两栏之后主路径（配置 → 提问 → 结果）始终在左栏一条线上。

      断点用 lg（1024px）而不是 xl（1280px）：1280 起才分栏的话，1366×768 的笔记本
      去掉浏览器边框就落在 1200 上下，正好永远看不到分栏——实际表现就是「一路往下堆」。
      右栏是 380px 固定列，1024 宽时左栏仍有 ~550px，够读正文。
    -->
    <div class="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_360px] xl:grid-cols-[minmax(0,1fr)_380px]">
      <div class="min-w-0 space-y-6">
    <!-- ③ 召回结果 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">召回结果</h2>
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

      <p v-else-if="result === null && researchResult === null" class="text-sm text-slate-400">
        还没发起检索：先选 Key、勾库、写问题，再点「检索」。
      </p>

      <!--
        Research 的产物多一栏 `answer`：它是引用块的渲染串，四条硬校验任一不过就是 null
        （材料照给）。所以这里「answer 为空」不是失败，只是没有合规引用——把停止原因与
        覆盖缺口摆在同一行，读的人一眼知道为什么短了。
      -->
      <div
        v-if="researchResult !== null"
        class="rounded border border-line bg-slate-50 px-4 py-3"
      >
        <p class="text-xs text-muted">
          停止原因
          <span class="font-mono text-slate-600">{{ researchResult.metadata.stop_reason }}</span>
          · 子检索 {{ researchResult.rounds.searches }} 次 · 证据
          <span class="font-mono text-slate-600">{{ researchResult.metadata.evidence_state }}</span>
        </p>
        <p
          v-if="researchResult.answer === null"
          class="mt-2 text-sm text-slate-600"
        >
          没有通过校验的引用块，<span class="font-mono text-xs">answer</span> 为空。
        </p>
        <p v-else class="mt-2 whitespace-pre-wrap break-words text-sm leading-[1.8] text-slate-800">
          {{ researchResult.answer }}
        </p>
        <p
          v-if="researchResult.coverage.gaps.length > 0"
          class="mt-1.5 text-xs text-[#7a5b00]"
        >
          未覆盖的要点：{{ researchResult.coverage.gaps.join('、') }}
        </p>
      </div>

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

      <ul v-if="chunks.length > 0" class="divide-y divide-line">
        <!--
          每条结果重排成「主行 / 正文 / 底注」三段。旧版把 5 个分数指标铺成两行 dl、
          再把两个 UUID 铺成一行，整块看下来是**一堆元数据而没有正文**——`chunk.content`
          后端一直在返回，却从没被渲染过。人来做检索是为了看命中什么，不是看分数表。
        -->
        <li v-for="(chunk, index) in chunks" :key="chunk.chunk_id" class="py-5">
          <div class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <div class="flex min-w-0 items-baseline gap-2.5">
              <span
                class="shrink-0 rounded-full bg-accent-soft px-2 py-0.5 font-mono text-xs font-medium text-ink"
              >
                {{ index + 1 }}
              </span>
              <h3 class="truncate text-[13px] font-semibold text-ink">
                {{ chunk.title || '（无标题）' }}
              </h3>
              <span v-if="chunk.heading_path !== null" class="truncate text-xs text-muted">
                › {{ chunk.heading_path }}
              </span>
            </div>

            <!-- 分数只留三个：主排序分大、两路分解小。名次留在底注。 -->
            <div class="flex shrink-0 items-baseline gap-3">
              <span class="font-mono text-[13px] font-medium text-ink">
                {{ formatScore(chunk.score) }}
              </span>
              <span class="font-mono text-xs text-muted">
                语义 {{ formatPercent(chunk.vector_score) }}
              </span>
              <span class="font-mono text-xs text-muted">
                关键词 {{ formatScore(chunk.bm25_score) }}
              </span>
            </div>
          </div>

          <!-- 命中正文。保留换行与空白：切片的形状（列表、表格行）是它的一半信息。 -->
          <p class="mt-2.5 whitespace-pre-wrap break-words text-[13px] leading-[1.8] text-slate-700">
            {{ chunk.content }}
          </p>

          <div class="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
            <span v-if="chunk.expansion !== null">扩展：{{ chunk.expansion }}</span>
            <span>语义名次 {{ formatRank(chunk.vector_rank) }}</span>
            <span>关键词名次 {{ formatRank(chunk.bm25_rank) }}</span>
            <span :title="chunk.chunk_id">切片 {{ shortId(chunk.chunk_id) }}</span>
            <span :title="chunk.document_id">文档 {{ shortId(chunk.document_id) }}</span>
            <span v-if="chunk.kb_id !== null" :title="chunk.kb_id">库 {{ shortId(chunk.kb_id) }}</span>
            <!-- 这条切片契约里的**每一个**字段（含卡片没展示的 rrf_score / section_id / source_chunk_ids）。 -->
            <button
              type="button"
              class="text-brand underline underline-offset-2 transition hover:text-ink"
              @click="toggleChunkFields(chunk.chunk_id)"
            >
              {{ expandedChunks.has(chunk.chunk_id) ? '收起字段' : '全部字段' }}
            </button>
          </div>

          <div
            v-if="expandedChunks.has(chunk.chunk_id)"
            class="mt-2 rounded-control bg-surface-muted px-3 py-2 text-xs"
          >
            <ResponseFields :value="chunk" />
          </div>
        </li>
      </ul>


    </el-card>
      </div>

      <!-- 右栏：诊断。默认就在视线外，不抢主路径的注意力。 -->
      <div class="min-w-0 space-y-6">
    <!--
      右栏：**这次检索的诊断**（运行摘要 / 六段耗时 / 打分）。
      这三块原来塞在「召回结果」卡底部，把正文挤到上面去了。它们回答的是
      「这次为什么这样跑」「哪一段慢」「这次结果好不好」，都是同一次检索的旁注，
      所以合成一张卡放在右栏，与正文并排而不是压在正文下面。
    -->
    <section class="card px-5 py-4">
      <h2 class="section-title">这次的诊断</h2>
      <div class="mt-3 space-y-3">
        <!--
          运行摘要收进结果卡底部、默认收起。它原来自己占一张卡，与结果并列——但它是
          「这次为什么这样跑」的元信息，读结果的当下不需要，需要一个入口就够了。
        -->
        <div class="border-t border-line pt-3">
          <button
            type="button"
            class="flex items-center gap-1.5 text-xs text-muted transition hover:text-ink"
            @click="summaryOpen = !summaryOpen"
          >
            <span class="text-[9px]">{{ summaryOpen ? '▼' : '▶' }}</span>
            运行摘要
          </button>
          <RunSummary v-if="summaryOpen && result !== null" class="mt-3" :metadata="result.metadata" />
          <p v-if="summaryOpen && result === null" class="mt-2 text-xs text-muted">
            跑一次检索后显示
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
            class="flex items-center gap-1.5 text-xs text-muted transition hover:text-ink"
            @click="waterfallOpen = !waterfallOpen"
          >
            <span class="text-[9px]">{{ waterfallOpen ? '▼' : '▶' }}</span>
            六段耗时
          </button>

          <div v-if="waterfallOpen" class="mt-3">
            <!-- 有这次检索的响应时：一键看，不用抄 UUID（与评分控件共用同一个反查结果）。 -->
            <template v-if="ratingLogId !== null">
              <p class="text-xs text-muted">
                这次检索
                <span class="font-mono text-slate-500">{{ ratingLogId.slice(0, 8) }}…</span>
              </p>
              <div class="mt-2">
                <el-button size="small" :loading="waterfallLoading" @click="loadWaterfall">
                  看这次检索的瀑布
                </el-button>
              </div>
            </template>
            <!-- 没有响应（例如只在工单里拿到一个 log_id）时，才退回手填。 -->
            <template v-else>
              <p class="text-xs text-muted">
                手填 <code class="rounded bg-slate-100 px-1 font-mono text-slate-500">log_id</code>
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
              class="mt-2 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
            >
              {{ waterfallError }}
            </p>
            <p v-else-if="ratingLogId === null && trimmedLogId === ''" class="mt-2 text-xs text-muted">
              先填 log_id
            </p>
            <StageWaterfall v-else-if="waterfall !== null" class="mt-3" :data="waterfall" />
          </div>
        </div>

        <!--
          评分（#106）：与运行摘要、瀑布图并列的第三个入口。
          它绑定的是**当前这次检索**（trace_id → 反查 log_id），不需要手填 UUID；
          观测开关不参与可用性判断——反馈以本地为准，观测栈挂了照样能打分。
        -->
        <div class="border-t border-line pt-3">
          <p class="text-xs font-medium text-slate-600">打分</p>

          <p v-if="ratingDisabledCopy !== ''" class="mt-2 text-xs text-muted">
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
              <span class="ml-2 text-xs text-muted">
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
              <span v-if="ratingSaved !== null" class="text-xs text-emerald-700">
                已评 {{ ratingSaved }} 分（再提交一次可改分）
              </span>
            </div>
            <p
              v-if="ratingError !== null"
              class="mt-2 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
            >
              {{ ratingError }}
            </p>
          </div>
        </div>
      </div>
    </section>

    <!--
      完整响应字段：这次回包的**全部字段**，单独一张卡，贴在「这次的诊断」正下方。
      四类响应的形状各不相同（`metadata.retrieval` 里可能带 `rerank` / `query_optimization` /
      `decomposition`，Research 是另一套 `plan` / `rounds` / `coverage` / `citations`），
      所以这里递归摊开整棵树，而不是按接口维护一张「要显示哪些字段」的表——那张表天然会漏，
      而漏掉的往往正是排障要看的。列表那两栏单独交代条数：逐条字段在左栏各卡片的「全部字段」里。
    -->
    <section v-if="responseFields !== null" class="card px-5 py-4">
      <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 class="section-title">完整响应字段</h2>
        <span class="flex items-center gap-3 text-xs">
          <button
            type="button"
            class="text-muted underline underline-offset-2 transition hover:text-ink"
            @click="expandTree(true)"
          >
            全部展开
          </button>
          <button
            type="button"
            class="text-muted underline underline-offset-2 transition hover:text-ink"
            @click="expandTree(false)"
          >
            全部收起
          </button>
          <button
            type="button"
            class="text-brand underline underline-offset-2 transition hover:text-ink"
            @click="copyResponseJson"
          >
            复制 JSON
          </button>
        </span>
      </div>
      <!--
        折叠树而不是整棵摊开：`/rewrite` 一次能回 50 条切片、`decomposition` 能拆五六条
        子问题，全部摊开会把这一栏撑成几屏，反而什么都看不出。默认开到第二层
        （顶层字段 + `metadata` 下各分组各占一行、项数写在标头上），要细节再点开。
      -->
      <div class="mt-2.5 rounded-control bg-surface-muted px-3 py-2.5 text-xs">
        <ResponseFields
          :value="responseFields"
          :default-open-depth="1"
          :expand-all="expandAll"
          :serial="expandSerial"
        />
      </div>
    </section>
      </div>
    </div>

    <!-- ⑨ 数据面接口清单：通栏。它是一张四列表格，塞进 380px 的右栏会被挤成一列字。 -->
    <CollapsibleCard
      title="能调的接口"
      :default-open="false"
      :summary="`${DATA_PLANE_ENDPOINTS.length} 条`"
    >
      <template #actions>
        <span class="chip">只读 {{ trialableCount }} 条可试</span>
      </template>
      <DataPlanePanel :kb-id="firstSelectedKbId" :has-key="keyApplied" />
    </CollapsibleCard>


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
