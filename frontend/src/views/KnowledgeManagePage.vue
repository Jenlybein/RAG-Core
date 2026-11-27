<script setup lang="ts">
/**
 * 知识库管理：一个页面管完「有哪些库、库里有什么、对库和文档做什么」。
 *
 * 合并的原因：原来「建库与上传」和「知识库列表」是两页，用户描述同一件事（我有哪些库、
 * 往哪个库传东西）却要在两个入口之间来回跳；而两页各自都只讲了一半——前者不能看库里的
 * 文档，后者不能建库。现在一页：列表是骨架，详情弹窗是库内文档的工作区。
 *
 * 几条刻意的设计：
 *
 * - **整行可点 = 详情**。列表行最想干的事就是「看看里面有什么」，让它成为一个可点的目标，
 *   比只给一个小按钮好点得多；操作列仍然保留显式入口，键盘与习惯点击的人都能用。
 * - **删除只对空库**。后端对非空库回 20015 而不是级联删除（级联要跨清向量/清关键词/
 *   删文件/删记录四件事，中途失败就留下一半一半的状态）。确认框把这条说清楚，别让
 *   用户以为是「删除失败」。
 * - **「改」= 重建索引**。仓库里没有「改文档标题 / 改正文」的接口，能改的只有切块结果，
 *   所以操作列给的是「重建」而不是编辑入口；对已成功的文档重建需要 force=true，
 *   等于丢掉一份还能用的索引，因此必须二次确认。
 *
 * 读与写都走**控制面会话门**（`services/consoleKb.ts`），本页不 import `lib/dataHttp`。
 */
import { Loading, Search } from '@element-plus/icons-vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import ConfirmDialog from '../components/ConfirmDialog.vue'
import KbSettingsDialog from '../components/KbSettingsDialog.vue'
import CreateKbDialog from '../components/CreateKbDialog.vue'
import DocumentVersionsDialog from '../components/DocumentVersionsDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import UploadPickerDialog from '../components/UploadPickerDialog.vue'
import {
  useUploadDocuments,
  type UploadFileItem,
  type UploadResultRow,
  type UploadRowStatus,
} from '../composables/useUploadDocuments'
import LiveStatus from '../components/LiveStatus.vue'
import {
  PAGE_SIZE_OPTIONS,
  useKbDocumentsFilter,
  useKbDocumentsQuery,
  useKbListQuery,
} from '../composables/useKbQueries'
import { errorText } from '../lib/errorText'
import {
  deleteDocumentConsole,
  deleteKbConsole,
  fetchDocumentContentConsole,
  KB_QUERY_PREFIX,
  reindexDocumentConsole,
  rebuildKbConsole,
  uploadConsole,
} from '../services/consoleKb'
import type { DocumentContent } from '../types/document'
import type { BuildSummary, KbDocumentRow, KbSummary } from '../types/knowledgeBase'
import { DOCUMENT_STATUS, isDocumentBusy } from '../utils/documentStatus'
import { formatTokens } from '../utils/usageFormat'

const SPLIT_LABELS: Record<string, string> = {
  structure: '结构切块',
  size: '窗口切块',
}

const STATUS_TEXT: Record<UploadRowStatus, string> = {
  pending: '上传中…',
  success: '上传成功',
  // 回执里的 4 是「已收下、在排队」，3 是「Worker 已经接走、在索引」——都不是失败，
  // 也都不是「0 个切片」。两种说法只用「排队中」与「索引中」这两个词（GLOSSARY）。
  queued: '已提交，排队中',
  processing: '已受理，索引中',
  failed: '上传失败',
  unknown: '未知状态',
}

/**
 * el-table 的插槽只把行标成 `DefaultRow`（不是我们的类型）。在边界处做一次形状判定，
 * 比在模板里堆类型断言可读得多——而且判定本身就是安全网：把库对象当文档发删除请求，
 * 在后端是一次 20016，在这里就是被拦下的一次空操作。
 */
function isKbRow(row: unknown): row is KbSummary {
  // `document_count` 而不是 `documents`：列表摘要**不再带文档数组**（第 17 步 #227）。
  // 判定条件跟着契约走——拿旧字段判会让每次点击都静默变成空操作。
  return typeof row === 'object' && row !== null && 'kb_id' in row && 'document_count' in row
}

function isDocRow(row: unknown): row is KbDocumentRow {
  return typeof row === 'object' && row !== null && 'document_id' in row
}

const router = useRouter()
const queryClient = useQueryClient()

/**
 * 列表与详情是**两条查询、两种节奏**（第 17 步 #227）：
 *
 * - 列表只读库摘要（四个数字 + 构建状态），空闲时 60 秒慢查一次；
 * - 详情只在弹窗打开时启用，**打开即取**——它以前没有自己的请求，只能读列表上一次轮询的
 *   快照，于是「进详情总要按 F5 才看到真状态」。
 */
const { query: listQuery, items: kbs, mode: listMode, updatedAt: listUpdatedAt } = useKbListQuery()
const listPending = listQuery.isPending
const listError = listQuery.error
const refetchList = listQuery.refetch

/** 容量占用百分比；上限为 0 时按 0 处理（那是异常配置，不该除以零让页面炸掉）。 */
function usagePercent(row: KbSummary): number {
  const usage = row.token_usage
  if (!usage || usage.limit <= 0) return 0
  return Math.round((usage.used / usage.limit) * 100)
}

/**
 * 构建进度百分比。
 *
 * 两个边界都必须显式处理，否则进度条会给错误信号：`planned === 0` 时回 0 而不是 NaN
 * （空库建版本是合法情形，`0 / 0` 不是）；`done === planned` 时回 100（断言「满了」，
 * 而不是留一格空白让人以为还差一点）。
 */
function progressPercent(progress: BuildSummary): number {
  if (progress.planned <= 0) return 0
  const ratio = (progress.done / progress.planned) * 100
  return Math.max(0, Math.min(100, Math.round(ratio)))
}

// 库名搜索仍在客户端：列表是整个租户的（十几到几十条），没有搜索参数的后端接口，
// 前端不能自己发明一个 query。文档搜索不一样——那是分页的，必须交给服务端。
const keyword = ref('')
const filteredKbs = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (needle === '') return kbs.value
  return kbs.value.filter((kb) => kb.name.toLowerCase().includes(needle))
})

/**
 * 写操作之后刷新：**失效整组读模型**（列表 / 详情 / 构建都在 `['knowledge-bases']` 前缀下）。
 *
 * 为什么用失效而不是 `refetch`：轮询、弹窗关闭后的即时刷新、手动刷新三条路径必须落在同一份
 * 缓存上，否则它们会互相覆盖（上一版就是两份状态互相对打）。
 */
async function refresh(): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: KB_QUERY_PREFIX })
}

// ───────── 详情弹窗：自己的查询、自己的节奏 ─────────
const detailKbId = ref<string | null>(null)
const detailOpen = ref(false)
const detailFilter = useKbDocumentsFilter()

/**
 * 详情一条查询：**打开即取**（`enabled` 由 `detailKbId` 控制，键里带页码与筛选）。
 *
 * 这一条是本轮「进详情要按 F5」的正面回答：它有请求，所以永远是打开那一刻的数据；
 * 响应带 `generated_at`，界面上就写得出「更新于 14:03:12」。
 */
const {
  query: detailQuery,
  payload: detailPayload,
  mode: detailMode,
  updatedAt: detailUpdatedAt,
} = useKbDocumentsQuery(detailKbId, detailFilter)

/**
 * 四个筛选参数单独拆出来给模板用：`useKbDocumentsFilter()` 返回的是装在普通对象里的 ref，
 * 模板不会自动解包对象内的 ref（只有顶层 ref 才会）——所以要么到处写 `.value`，
 * 要么在这里拆一次。这里拆。
 */
const docKeyword = detailFilter.keyword
const detailStatus = detailFilter.status
const detailPage = detailFilter.page
const detailPageSize = detailFilter.pageSize

const detailKb = computed<KbSummary | null>(() => detailPayload.value?.kb ?? null)
const detailBuild = computed<BuildSummary | null>(() => detailPayload.value?.build ?? null)
const detailDocuments = computed<KbDocumentRow[]>(() => detailPayload.value?.documents ?? [])
const detailTotal = computed(() => detailPayload.value?.total ?? 0)
const detailPending = detailQuery.isPending
const detailError = detailQuery.error
const detailFetching = detailQuery.isFetching

/**
 * 当前页里「还没被这一版收进去」的篇数。
 *
 * 只数 `pending`：`failed` 那一批是**已经有结论**的（这一版明确没收它），把它们算进
 * 「还剩多少」会让人一直等一个不会变的数。进度条的分母/分子另有构建行自己记的
 * `done` / `planned`，这一格只是「你正在看的这一页还有几篇没落地」。
 */
const detailOutstanding = computed(
  () => detailDocuments.value.filter((row) => row.build_state === 'pending').length,
)

function openDetail(row: unknown): void {
  if (!isKbRow(row)) return
  detailKbId.value = row.kb_id
  // 换库就回第一页并清掉筛选：带着上一个库的关键字打开新库，多半是空表。
  detailFilter.keyword.value = ''
  detailFilter.status.value = undefined
  detailFilter.page.value = 1
  detailOpen.value = true
}

// ───────── 查看原文 ─────────
const contentOpen = ref(false)
const contentLoading = ref(false)
const contentError = ref('')
const contentData = ref<DocumentContent | null>(null)

/**
 * 「查看」= 读原文。
 *
 * 不复用 vue-query：这是一次性的、按需的读，缓存下来只会让人看到过期的原文
 * （刚重建过索引的文档，用户最想看的恰恰是「现在这份是什么」）。
 */
async function openContent(row: unknown): Promise<void> {
  if (!isDocRow(row)) return
  contentData.value = null
  contentError.value = ''
  contentOpen.value = true
  contentLoading.value = true
  try {
    contentData.value = await fetchDocumentContentConsole(row.document_id)
  } catch (error) {
    // 20016（跨租户/已删）、20023（没有解析器）、20024（解析失败）都走这里：
    // 原文读不出来是**业务结果**，把后端原文展示出来比一句「加载失败」有用得多。
    contentError.value = errorText(error)
  } finally {
    contentLoading.value = false
  }
}

// ───────── 版本列表与回滚 ─────────
const versionsOpen = ref(false)
const versionsDocId = ref<string | null>(null)
const versionsDocTitle = ref('')
/**
 * 打开版本弹窗时那一行的状态（四态原值，不是布尔）。
 *
 * 弹窗自己按 `isDocumentBusy` 判「给不给回滚」：只有索引中（3）可能真在跑。把它折成布尔
 * 交给弹窗，就等于让「三种状态压成两种」发生在页面里，而压错的代价是一个本该能点的按钮消失。
 */
const versionsDocumentStatus = ref<number | null>(null)

/**
 * 版本弹窗认 `document_id`（不是整行）：树每 3 秒被轮询替换，存下来的行会是一份旧快照
 * ——「刚回滚完，弹窗里还是旧的当前稿」正是那种只在轮询窗口里出现的假象。
 *
 * 状态取的是点击那一刻的行状态；文档在后端开始重建后行会变成排队中（4），而排队中仍然
 * 允许回滚（后端守卫只看租约），所以这里不需要再跟着树刷新。
 */
function openVersions(row: unknown): void {
  if (!isDocRow(row)) return
  versionsDocId.value = row.document_id
  versionsDocTitle.value = row.title
  versionsDocumentStatus.value = row.status
  versionsOpen.value = true
}

function afterRolledBack(): void {
  // 回滚只是**受理**（回执 status=4 排队中），切块要重建；进度与终态都靠树轮询，这里先刷一次。
  void refresh()
}

// ───────── 新建 / 上传弹窗 ─────────
const createOpen = ref(false)
const uploadOpen = ref(false)
const uploadPresetKbId = ref('')

const kbOptions = computed(() =>
  kbs.value.map((kb) => ({
    value: kb.kb_id,
    label: `${kb.name}（${kb.document_count} 篇）`,
  })),
)

function openUpload(kbId: string): void {
  uploadPresetKbId.value = kbId
  uploadOpen.value = true
}

/** 改切块设置（第 16 步 #207）：弹窗自己读预览、自己二次确认、成功后通知刷新。 */
const settingsKbId = ref<string | null>(null)

function openSettings(row: unknown): void {
  if (!isKbRow(row)) return
  settingsKbId.value = row.kb_id
}

function afterCreated(): void {
  void refresh()
}

function continueUploadTo(kbId: string): void {
  void refresh()
  openUpload(kbId)
}

// ───────── 上传（编排在 composable 里，页面只管何时开始与结果展示）─────────
const uploader = useUploadDocuments({
  upload: (item, targetKbId) =>
    // 标题用文件基名：选文件夹时 item.fileName 含相对路径，整条路径当文档标题只是噪声。
    uploadConsole({ kb_id: targetKbId, title: item.file.name, file: item.file }),
})
const { results, succeededCount, failedCount, uploadAll } = uploader
const showResults = computed(() => results.value.length > 0)

async function handleUpload(targetKbId: string, files: UploadFileItem[]): Promise<void> {
  await uploadAll(targetKbId, files)
  // 上传只登记 + 投递，行状态要等轮询看到终态；这里先刷一次让新文档立刻出现在详情里。
  void refresh()
}

function rowErrorText(row: UploadResultRow): string {
  if (row.errorMessage === null) return ''
  if (row.errorCode === null || row.errorCode === -1) return row.errorMessage
  return `${row.errorMessage}（业务码 ${row.errorCode}）`
}

// ───────── 删除知识库 / 删除文档 / 重建索引 ─────────
type PendingAction =
  | { kind: 'deleteKb'; kb: KbSummary }
  | { kind: 'deleteDoc'; document: KbDocumentRow }
  | { kind: 'reindex'; document: KbDocumentRow }
  | { kind: 'rebuild'; kb: KbSummary }

const pendingAction = ref<PendingAction | null>(null)
const confirmOpen = ref(false)

const dialogTitle = computed(() => {
  const action = pendingAction.value
  if (action === null) return ''
  if (action.kind === 'deleteKb') return '删除知识库'
  if (action.kind === 'deleteDoc') return '删除文档'
  if (action.kind === 'rebuild') return '重建这个知识库'
  // 排队中不是「重试」（没有失败过），而是「替换排队」——换一份内容或换一次解析。
  if (action.document.status === DOCUMENT_STATUS.QUEUED) return '重新提交（替换排队）'
  return action.document.status === DOCUMENT_STATUS.SUCCESS ? '重建索引（覆盖现有索引）' : '重试索引'
})

const dialogMessage = computed(() => {
  const action = pendingAction.value
  if (action === null) return ''
  if (action.kind === 'deleteKb') {
    const count = action.kb.document_count
    if (count === 0) return `确认删除空知识库《${action.kb.name}》？删除后无法恢复。`
    // 非空库后端会拒绝；提前说清规则与做法，比让用户撞一次 20015 更好。
    return `《${action.kb.name}》里还有 ${count} 篇文档，删不掉。先在这个库的详情里把文档删空，再删库。`
  }
  if (action.kind === 'rebuild') {
    const count = action.kb.document_count
    // 重建 = 产出新版本并激活：全库重嵌入 + 重写 ES，代价是服务器时间与钱。这句必须说清，
    // 否则「一次误点」在用户心里只是一次刷新。
    return (
      `按**当前切块设置**重建《${action.kb.name}》里的 ${count} 篇文档？` +
      '这会新建一个索引版本、跑完全部文档后自动切换过去（全库重嵌入，需要一段时间）。' +
      '重建期间这个库的检索仍走旧版本，不受影响。'
    )
  }
  if (action.kind === 'deleteDoc') {
    // 排队中删除 = 撤回排队：说清楚「迟到的任务不会再切」，否则用户会担心删除删不干净。
    if (action.document.status === DOCUMENT_STATUS.QUEUED) {
      return `确认撤回《${action.document.title}》的排队？它会连同已落库的记录一起被清掉，后台不会再为它切块。`
    }
    return `确认删除《${action.document.title}》？后端会清除切片、删除原文与记录，无法撤销。`
  }
  // 成功的文档已有可用索引：force=true 会丢掉它，语义要说清。
  if (action.document.status === DOCUMENT_STATUS.SUCCESS) {
    return `《${action.document.title}》已经有可用索引。重建会用新的切块结果覆盖它（旧索引会被丢弃），确认继续？`
  }
  // 排队中的文档还没被 Worker 接走，重新提交只是替换这一次排队，不会两个任务同时跑。
  if (action.document.status === DOCUMENT_STATUS.QUEUED) {
    return `《${action.document.title}》正在排队中。重新提交会替换掉当前这次排队（旧任务不会再切一遍），确认继续？`
  }
  // 重建回执恒为 status=4（排队中）：说清楚「只是重新排队」，别让用户以为点完就恢复成功。
  return `重新提交《${action.document.title}》的索引任务？提交后状态回到「排队中」，由后台接着处理。`
})

const dialogConfirmText = computed(() => {
  const action = pendingAction.value
  if (action === null) return '确认'
  if (action.kind === 'rebuild') return '开始重建'
  if (action.kind === 'reindex') {
    if (action.document.status === DOCUMENT_STATUS.QUEUED) return '替换排队'
    return action.document.status === DOCUMENT_STATUS.SUCCESS ? '覆盖重建' : '重试'
  }
  return '删除'
})

const dialogDanger = computed(
  () =>
    pendingAction.value?.kind !== 'reindex' && pendingAction.value?.kind !== 'rebuild',
)
/** 非空库不允许点删除确认：规则来自后端，这里只是别给一个注定失败的按钮。 */
const confirmDisabled = computed(
  () => pendingAction.value?.kind === 'deleteKb' && pendingAction.value.kb.document_count > 0,
)

function askDeleteKb(row: unknown): void {
  if (!isKbRow(row)) return
  pendingAction.value = { kind: 'deleteKb', kb: row }
  confirmOpen.value = true
}

function askDeleteDoc(row: unknown): void {
  if (!isDocRow(row)) return
  pendingAction.value = { kind: 'deleteDoc', document: row }
  confirmOpen.value = true
}

function askRebuild(row: unknown): void {
  if (!isKbRow(row)) return
  pendingAction.value = { kind: 'rebuild', kb: row }
  confirmOpen.value = true
}

function askReindex(row: unknown): void {
  if (!isDocRow(row)) return
  pendingAction.value = { kind: 'reindex', document: row }
  confirmOpen.value = true
}

function closeDialog(): void {
  confirmOpen.value = false
  pendingAction.value = null
}

const deleteKbMutation = useMutation({
  mutationFn: (kb: KbSummary) => deleteKbConsole(kb.kb_id),
  onSuccess: () => {
    ElMessage.success('知识库已删除')
    closeDialog()
    void refresh()
  },
  onError: (error) => {
    // 20015 的原文要说给用户听（先删文档），不要翻译成「删除失败」。
    ElMessage.error(errorText(error))
  },
})

const deleteDocMutation = useMutation({
  mutationFn: (document: KbDocumentRow) => deleteDocumentConsole(document.document_id),
  onSuccess: () => {
    ElMessage.success('文档已删除')
    closeDialog()
    void refresh()
  },
  onError: (error) => {
    ElMessage.error(errorText(error))
  },
})

const reindexMutation = useMutation({
  mutationFn: (document: KbDocumentRow) =>
    // 已成功的文档必须显式 force：后端否则回 20027，替用户保住还能用的索引。
    // 排队中 / 失败 / 索引中（租约已过期那种）都不带 force——它们本来就在可重建那一档。
    reindexDocumentConsole(document.document_id, {
      force: document.status === DOCUMENT_STATUS.SUCCESS,
    }),
  onSuccess: () => {
    ElMessage.success('已重新提交索引，稍后自动刷新')
    closeDialog()
    void refresh()
  },
  onError: (error) => {
    ElMessage.error(errorText(error))
  },
})

/**
 * 重建这个库（第 17 步）：**按当前切块设置**产出新版本，建完自动激活。
 *
 * ## 为什么不是「把每篇文档重新投一遍」
 *
 * 第一版就是那么做的（逐篇调 `documents/{id}/reindex`），而用户点完当场发现：「需重建」
 * 那条提示一直挂着。原因在语义里——逐篇重建写回的是**当前生效版本**，它的参数快照与版本名
 * 都不变。于是文档确实都重切了一遍，但「生效版本用的是哪套参数」这件事没变，提示当然消不掉；
 * 顺带还让生效版本里混进了两种切法的切片（`params` 快照从此失真）。
 *
 * 要让新设置真正生效只有一条路：**产出新版本并激活它**。那就是
 * `POST /console/knowledge-bases/{id}/rebuild`（后端 `rebuild_kb`：不改设置，只写一条
 * `kb.rebuild` 审计 + 起一个 `auto_activate=True` 的构建）。所以这个按钮的语义是
 * 「重切整个库」，不是「重排一批任务」。
 *
 * ## 为什么只转被点的那个库
 *
 * 一次重建要跑完整个库（几十篇就是几十次嵌入）。`rebuildingKbId` 只让被点那张卡片的按钮
 * 转圈；用一个全页布尔量会让**所有**库的按钮一起转，用户会以为点了全部。
 */
const rebuildingKbId = ref<string | null>(null)

async function rebuildKb(kb: KbSummary): Promise<void> {
  rebuildingKbId.value = kb.kb_id
  try {
    const receipt = await rebuildKbConsole(kb.kb_id)
    ElMessage.success(
      `已开始重建（${receipt.build_version}，${receipt.documents_planned} 篇），完成后自动生效`,
    )
    void refresh()
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    rebuildingKbId.value = null
  }
}

const acting = computed(
  () =>
    deleteKbMutation.isPending.value ||
    deleteDocMutation.isPending.value ||
    reindexMutation.isPending.value,
)

function confirmAction(): void {
  const action = pendingAction.value
  if (action === null) return
  if (action.kind === 'deleteKb') deleteKbMutation.mutate(action.kb)
  else if (action.kind === 'deleteDoc') deleteDocMutation.mutate(action.document)
  else if (action.kind === 'rebuild') {
    // 整库重建要过确认框：它一次覆盖全库（旧版本会被归档，新版本跑完自动接管）。
    closeDialog()
    void rebuildKb(action.kb)
  } else reindexMutation.mutate(action.document)
}

function goRetrieve(kbId: string): void {
  void router.push({ path: '/retrieve', query: { kb_id: kbId } })
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader title="知识库管理">
      <template #actions>
        <el-button type="primary" @click="createOpen = true">新建知识库</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <span class="th-label">{{ kbs.length }} 个知识库</span>
          <LiveStatus
            :updated-at="listUpdatedAt"
            :mode="listMode"
            :refreshing="listQuery.isFetching.value"
            @refresh="refresh()"
          />
          <el-input v-model="keyword" class="w-64" placeholder="搜索知识库" clearable>
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>
      </template>

      <!-- 在途提示：只在确实有文档排队中或索引中时出现，不是常驻横幅。 -->
      <div
        v-if="listMode === 'live'"
        class="mb-4 flex items-center gap-2 rounded-control border border-[#f0dfae] bg-[#fff8e6] px-3 py-2 text-xs text-[#7a5b00]"
      >
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>有构建在跑或有文档在排队/索引中，正在自动刷新</span>
      </div>

      <p v-if="listPending" class="py-8 text-center text-sm text-muted">正在读取知识库…</p>

      <!-- 读取失败不假装空列表：空列表会让人以为库被删了。 -->
      <div
        v-else-if="listError !== null"
        class="rounded-control border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
      >
        <p class="m-0">{{ errorText(listError) }}</p>
        <el-button class="mt-2" size="small" @click="refetchList()">重试</el-button>
      </div>

      <div v-else-if="kbs.length === 0" class="py-12 text-center">
        <p class="text-base font-semibold text-ink">还没有知识库</p>
        <el-button type="primary" class="!mt-5" @click="createOpen = true">新建知识库</el-button>
      </div>

      <p v-else-if="filteredKbs.length === 0" class="py-8 text-center text-sm text-muted">
        没有名称匹配「{{ keyword.trim() }}」的知识库。
      </p>

      <!--
        库列表用卡片而不是表格：知识库是「一个个实体」（各自的格式、窗口、文档量），
        表格把它们压成一行行数字，个数少时整页空得慌。卡片还给「进详情」一个大的点击面。
      -->
      <div v-else class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <!--
          外层用 div 而不是 button：卡片里还有「上传 / 检索 / 删除」三个按钮，button 套 button
          是非法结构（浏览器会重排 DOM，点击行为变得难预测）。用 role+tabindex 保留键盘可达。
        -->
        <div
          v-for="row in filteredKbs"
          :key="row.kb_id"
          role="button"
          tabindex="0"
          class="group flex cursor-pointer flex-col gap-3 rounded-control border border-line bg-surface p-4 text-left transition-colors hover:border-line-strong hover:bg-surface-muted/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-line-strong"
          @click="openDetail(row)"
          @keydown.enter="openDetail(row)"
          @keydown.space.prevent="openDetail(row)"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <p class="truncate text-[15px] font-semibold text-ink">{{ row.name }}</p>
              <p class="mt-0.5 truncate text-xs text-muted">
                {{ row.allowed_formats.join('、') || '（无格式）' }}
              </p>
            </div>
            <span class="chip shrink-0">{{ SPLIT_LABELS[row.split_strategy] ?? row.split_strategy }}</span>
          </div>

          <div class="flex items-end gap-5">
            <div>
              <p class="metric-value">{{ row.document_count }}</p>
              <p class="metric-label">篇文档</p>
            </div>
            <div>
              <p class="metric-value">{{ row.chunk_count }}</p>
              <p class="metric-label">个分块</p>
            </div>
            <div>
              <p class="metric-value">{{ row.chunk_size }}</p>
              <p class="metric-label">token 窗口</p>
            </div>
            <div>
              <p class="metric-value">{{ row.section_part_max_chars }}</p>
              <p class="metric-label">节段上限</p>
            </div>
          </div>

          <!--
            容量条（第 17 步 #221 / 用户故事 #13）：用户在**上传之前**就该知道还剩多少空间。

            `used` 是已落库的 token 合计——与容量硬拦同一个数。刻意不做「再加一篇会不会超」的
            预测：那需要知道下一篇多大，而那是上传之后才有的事。这里只回答「现在装了多少」。

            快满（≥90%）换成警示色、满了写「已满，需先删文档」：都是同一句话的两种语气，
            而「已满」时的处置（删文档）与「额度用尽」时的处置（等明天）是两句话——混在一起
            会让用户白删。
          -->
          <div
            v-if="row.token_usage"
            class="mt-2 flex items-center gap-2 text-xs"
            :class="usagePercent(row) >= 100 ? 'text-amber-700' : 'text-slate-500'"
          >
            <span class="shrink-0 tabular-nums">
              {{ formatTokens(row.token_usage.used) }} / {{ formatTokens(row.token_usage.limit) }}
              token
            </span>
            <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              <div
                class="h-full rounded-full"
                :class="usagePercent(row) >= 100 ? 'bg-amber-500' : usagePercent(row) >= 90 ? 'bg-orange-400' : 'bg-sky-500'"
                :style="{ width: `${Math.min(usagePercent(row), 100)}%` }"
              ></div>
            </div>
            <span v-if="usagePercent(row) >= 100" class="shrink-0 font-medium">已满，需先删文档</span>
            <span v-else class="shrink-0">余 {{ formatTokens(row.token_usage.limit - row.token_usage.used) }}</span>
          </div>

          <!--
            「重建中 37 / 500」（第 17 步 #216 / #227 改）：进度条读构建行自己记的
            documents_done / planned；旁边那行「N 篇未入新版本」直接来自失败明细。

            现在 `build` 也带**已有结论**的那一次（`ready` / `active` / `failed`），
            所以收尾之后还能说一句「v5 已生效」——以前构建一结束这一块整块消失，
            用户既看不出刚才成没成，也看不出新旧版本换没换。
          -->
          <div
            v-if="row.build && row.build.status === 'building'"
            class="mt-1 rounded-md bg-sky-50 px-2 py-1.5"
          >
            <div class="flex items-center justify-between gap-2 text-xs text-sky-800">
              <span>
                重建中 {{ row.build.done }} / {{ row.build.planned }}
                <span class="text-sky-600">（{{ row.build.version }}）</span>
              </span>
              <span v-if="row.build.failure_count > 0" class="font-medium text-amber-700">
                {{ row.build.failure_count }} 篇未入新版本
              </span>
            </div>
            <div
              class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-sky-100"
              role="progressbar"
              :aria-valuenow="progressPercent(row.build)"
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <div
                class="h-full rounded-full bg-sky-500 transition-[width] duration-500"
                :style="{ width: progressPercent(row.build) + '%' }"
              />
            </div>
            <ul v-if="row.build.failures.length > 0" class="mt-1 space-y-0.5">
              <li
                v-for="failure in row.build.failures"
                :key="failure.document_id"
                class="truncate text-[11px] text-amber-700"
              >
                · {{ failure.title }}（{{ failure.reason }}）
              </li>
            </ul>
            <p
              v-if="row.build.failure_count > row.build.failures.length"
              class="mt-0.5 text-[11px] text-muted"
            >
              还有 {{ row.build.failure_count - row.build.failures.length }} 篇未列出
            </p>
          </div>

          <!-- 收尾之后的那句话：这一版成没成、现在服务的是哪一版。 -->
          <p
            v-else-if="row.build && row.build.status === 'active'"
            class="mt-1 rounded-md bg-emerald-50 px-2 py-1 text-xs text-emerald-700"
          >
            {{ row.build.version }} 已生效（{{ row.build.done }} / {{ row.build.planned }} 篇）
            <span v-if="row.build.failure_count > 0" class="text-amber-700">
              · {{ row.build.failure_count }} 篇未入这一版
            </span>
          </p>
          <p
            v-else-if="row.build && row.build.status === 'failed'"
            class="mt-1 rounded-md bg-red-50 px-2 py-1 text-xs text-red-700"
          >
            上次构建未完成（{{ row.build.done }} / {{ row.build.planned }} 篇），检索仍走上一版。
          </p>

          <!-- 四态里最该被看见的那一个：有失败就要能一眼看到，而不是点进详情去数。 -->
          <p
            v-if="row.status_counts.failed > 0"
            class="mt-1 text-xs text-red-600"
          >
            {{ row.status_counts.failed }} 篇索引失败（点开详情看原因）
          </p>

          <!--
            「需重建」（第 16 步 #207）：切块设置是索引期旋钮，改了不重建，读到的还是旧切法。
            这条提示必须看得见——否则「我明明改了阈值」只能在评测数字上慢慢发现。
          -->
          <p
            v-if="row.needs_rebuild"
            class="mt-1 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-700"
          >
            当前切块设置与生效索引版本不一致，需要重建后才生效。
          </p>

          <!-- 按钮要阻止冒泡，否则会顺带触发整卡的「详情」 -->
          <div class="mt-1 flex flex-wrap items-center gap-1 border-t border-line pt-2.5" @click.stop>
            <el-button link type="primary" size="small" @click="openUpload(row.kb_id)">上传</el-button>
            <el-button link type="primary" size="small" @click="goRetrieve(row.kb_id)">检索</el-button>
            <el-button link type="primary" size="small" @click="openSettings(row)">设置</el-button>
            <!--
              重建（第 17 步）：按**当前**切块设置产出新版本，建完自动激活。

              它同时是「需重建」那条提示的配套动作——那条横幅说的是「生效版本的参数快照与当前
              设置不一致」，只有产出新版本才消得掉；逐篇重建写回的是旧版本，消不掉（第一版就
              踩了这个坑，用户点完发现提示还在）。

              `build.status === 'building'`（这个库正在跑构建）时禁用：并发构建由后端守
              （50001），这里只是别给一个注定失败的按钮。
            -->
            <el-button
              link
              type="primary"
              size="small"
              :disabled="
                rebuildingKbId !== null || row.build?.status === 'building' || row.document_count === 0
              "
              :loading="rebuildingKbId === row.kb_id"
              @click="askRebuild(row)"
            >
              重建
            </el-button>
            <el-button link type="danger" size="small" @click="askDeleteKb(row)">删除</el-button>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 本次上传回执：只在真传过之后出现 -->
    <el-card v-if="showResults" shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 class="section-title">本次上传</h2>
          </div>
          <div class="shrink-0 text-sm text-slate-600">
            <span>成功 {{ succeededCount }}</span>
            <span class="mx-2 text-slate-300">/</span>
            <span>失败 {{ failedCount }}</span>
          </div>
        </div>
      </template>
      <ul class="divide-y divide-line">
        <li v-for="row in results" :key="row.key" class="py-2.5">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span class="break-all text-sm font-medium text-ink">{{ row.fileName }}</span>
            <span class="text-sm text-slate-600">{{ STATUS_TEXT[row.status] }}</span>
          </div>
          <div class="mt-1 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
            <span class="break-all">document_id：{{ row.documentId ?? '—' }}</span>
            <span>chunk_count：{{ row.chunkCount === null ? '—' : row.chunkCount }}</span>
          </div>
          <p v-if="row.errorMessage !== null" class="mt-1 break-words text-xs text-danger">
            {{ rowErrorText(row) }}
          </p>
        </li>
      </ul>
    </el-card>

    <!-- ───────── 详情：库内文档的增删查改（自己的查询、自己的节奏）───────── -->
    <el-dialog v-model="detailOpen" append-to-body width="880px" :close-on-click-modal="false">
      <template #header>
        <div class="flex flex-wrap items-start justify-between gap-3 pr-6">
          <div class="min-w-0">
            <h2 class="text-[17px] font-semibold tracking-tight text-ink">
              {{ detailKb?.name ?? '知识库详情' }}
            </h2>
            <p v-if="detailKb !== null" class="mt-0.5 text-xs text-muted">
              {{ SPLIT_LABELS[detailKb.split_strategy] ?? detailKb.split_strategy }} ·
              {{ detailKb.document_count }} 篇 /
              {{ detailKb.chunk_count }} 块 · 窗口 {{ detailKb.chunk_size }} token
            </p>
            <!--
              弹窗里也必须有一句「数据更新于几点」：这个弹窗以前完全没有自己的请求，
              读到的是列表页上一次轮询的快照，而列表页在「全终态」时会静默停掉轮询——
              于是「已经刷新完了」与「停在十分钟前」在这屏长得一模一样，唯一出路是 F5。
            -->
            <LiveStatus
              class="mt-1"
              :updated-at="detailUpdatedAt"
              :mode="detailMode"
              :refreshing="detailFetching"
              @refresh="detailQuery.refetch()"
            />
          </div>
          <el-button
            v-if="detailKb !== null"
            type="primary"
            size="small"
            @click="openUpload(detailKb.kb_id)"
          >
            上传文档
          </el-button>
        </div>
      </template>

      <div class="space-y-3">
        <!--
          构建进度也在弹窗里（以前只在卡片上）：弹窗一开就把它盖住了，而它恰恰是
          「这个库到底在不在动」的唯一真信号——文档状态在构建期间几乎一直是「成功」。
        -->
        <div
          v-if="detailBuild !== null && detailBuild.status === 'building'"
          class="rounded-control bg-sky-50 px-3 py-2"
        >
          <div class="flex flex-wrap items-center justify-between gap-2 text-xs text-sky-800">
            <span>
              重建中 {{ detailBuild.done }} / {{ detailBuild.planned }}
              <span class="text-sky-600">（{{ detailBuild.version }}，完成后自动切换）</span>
            </span>
            <span v-if="detailOutstanding > 0" class="text-amber-700">
              本页还有 {{ detailOutstanding }} 篇未入这一版
            </span>
          </div>
          <div
            class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-sky-100"
            role="progressbar"
            :aria-valuenow="progressPercent(detailBuild)"
            aria-valuemin="0"
            aria-valuemax="100"
          >
            <div
              class="h-full rounded-full bg-sky-500 transition-[width] duration-500"
              :style="{ width: progressPercent(detailBuild) + '%' }"
            />
          </div>
        </div>
        <p
          v-else-if="detailBuild !== null && detailBuild.status === 'active'"
          class="rounded-control bg-emerald-50 px-3 py-1.5 text-xs text-emerald-700"
        >
          {{ detailBuild.version }} 已生效（{{ detailBuild.done }} / {{ detailBuild.planned }} 篇）
          <span v-if="detailBuild.failure_count > 0" class="text-amber-700">
            · {{ detailBuild.failure_count }} 篇未入这一版
          </span>
        </p>
        <p
          v-else-if="detailBuild !== null && detailBuild.status === 'failed'"
          class="rounded-control bg-red-50 px-3 py-1.5 text-xs text-red-700"
        >
          上次构建未完成（{{ detailBuild.done }} / {{ detailBuild.planned }} 篇），检索仍走上一版。
        </p>

        <div class="flex flex-wrap items-center gap-2">
          <!-- 关键字交给**服务端**（库可能几千篇，前端只拿一页）：输入有 300ms 防抖。 -->
          <el-input
            v-model="docKeyword"
            class="min-w-[200px] flex-1"
            placeholder="搜索文档标题（服务端筛选）"
            clearable
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-select
            v-model="detailStatus"
            class="w-32"
            placeholder="全部状态"
            clearable
          >
            <el-option label="成功" :value="1" />
            <el-option label="失败" :value="2" />
            <el-option label="索引中" :value="3" />
            <el-option label="排队中" :value="4" />
          </el-select>
        </div>

        <p v-if="detailError !== null" class="py-4 text-center text-sm text-red-600">
          {{ errorText(detailError) }}
        </p>

        <p v-else-if="detailPending" class="py-6 text-center text-sm text-slate-500">
          正在读取文档…
        </p>

        <p
          v-else-if="detailKb !== null && detailKb.document_count === 0"
          class="rounded-control border border-line px-3 py-6 text-center text-sm text-slate-500"
        >
          这个库还没有文档。
        </p>

        <p v-else-if="detailDocuments.length === 0" class="py-4 text-center text-sm text-slate-500">
          没有匹配的文档。
        </p>

        <template v-else>
          <el-table :data="detailDocuments" row-key="document_id" class="w-full">
            <el-table-column label="文档" min-width="220">
              <template #default="{ row }">
                <span class="break-all text-sm text-ink">{{ row.title }}</span>
                <p class="mt-0.5 text-xs text-muted">{{ row.source_type }}</p>
              </template>
            </el-table-column>

            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <StatusBadge :status="row.status" :error-message="row.error_message" />
              </template>
            </el-table-column>

            <!--
              第二根轴（第 17 步 #227）：它与**当前这一版构建**的关系。
              构建期间文档状态几乎一直是「成功」（每篇只有自己那一秒是「索引中」），
              所以「还在重建」这件事只能靠这一列说出来。
            -->
            <el-table-column label="本次构建" width="110">
              <template #default="{ row }">
                <span v-if="row.build_state === 'done'" class="text-xs text-emerald-700">
                  ✓ 已重建
                </span>
                <span v-else-if="row.build_state === 'pending'" class="text-xs text-amber-700">
                  ⏳ 待重建
                </span>
                <span v-else-if="row.build_state === 'failed'" class="text-xs text-red-600">
                  ✗ 未入版
                </span>
                <span v-else class="text-xs text-slate-400">—</span>
              </template>
            </el-table-column>

            <el-table-column label="分块数" width="90">
              <template #default="{ row }">
                <span class="text-sm text-slate-700">{{ row.chunk_count }} 块</span>
              </template>
            </el-table-column>

            <el-table-column label="操作" width="250">
              <template #default="{ row }">
                <div class="flex flex-wrap items-center gap-1">
                  <!-- 查看原文对排队中 / 索引中也可用：读不改状态。失败文档的原文恰恰是判断
                       「为什么解析出错」的唯一依据，所以它不跟写操作一起被藏起来。 -->
                  <el-button link type="primary" size="small" @click="openContent(row)">
                    查看原文
                  </el-button>
                  <!-- 版本列表同样只读：任何时候都能看「这篇文档改过几稿」。 -->
                  <el-button link type="primary" size="small" @click="openVersions(row)">
                    版本
                  </el-button>
                  <!-- 索引中（3）没有任何写操作：此刻重建 / 删除都会回 20025（有人在写这篇的切片）。
                       排队中（4）**可以删、可以重交**：那是撤回排队与替换排队（ADR-0034 决策一），
                       按钮照给；失败与成功也在这一档。 -->
                  <span v-if="isDocumentBusy(row.status)" class="text-xs text-slate-400">索引中</span>
                  <template v-else-if="row.status === 1 || row.status === 2 || row.status === 4">
                    <el-button link type="primary" size="small" @click="askReindex(row)">
                      重建
                    </el-button>
                    <el-button link type="danger" size="small" @click="askDeleteDoc(row)">
                      删除
                    </el-button>
                  </template>
                  <!-- 未知状态不提供任何写操作：后端将来新增状态时，宁可少一个入口也别乱删。 -->
                  <span v-else class="text-xs text-slate-400">—</span>
                </div>
              </template>
            </el-table-column>
          </el-table>

          <!-- 分页：库可能几千篇，一次全取会把这个弹窗做成几 MB 的表格。 -->
          <el-pagination
            class="justify-end"
            layout="total, sizes, prev, pager, next"
            :total="detailTotal"
            :current-page="detailPage"
            :page-size="detailPageSize"
            :page-sizes="[...PAGE_SIZE_OPTIONS]"
            @current-change="(page: number) => (detailPage = page)"
            @size-change="(size: number) => { detailPageSize = size; detailPage = 1 }"
          />
        </template>
      </div>

      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- ───────── 原文：只读，独立弹窗叠在详情之上 ───────── -->
    <el-dialog
      v-model="contentOpen"
      width="880px"
      append-to-body
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="pr-6">
          <h2 class="text-[17px] font-semibold tracking-tight text-ink">
            {{ contentData?.title ?? '原文' }}
          </h2>
          <p v-if="contentData !== null" class="mt-0.5 text-xs text-muted">
            {{ contentData.source_type }}
            <span v-if="contentData.source_filename"> · {{ contentData.source_filename }}</span>
            · 共 {{ contentData.length }} 字符
          </p>
        </div>
      </template>

      <div class="space-y-3">
        <p v-if="contentLoading" class="py-8 text-center text-sm text-slate-500">正在读取原文…</p>

        <!-- 读不出来也要说清是哪一步：20016 已删除 / 20023 没有解析器 / 20024 解析失败 -->
        <div
          v-else-if="contentError !== ''"
          class="rounded-control border border-red-200 bg-red-50 px-3 py-3 text-sm text-red-700"
        >
          <p class="m-0">{{ contentError }}</p>
        </div>

        <template v-else-if="contentData !== null">
          <div
            v-if="contentData.truncated"
            class="rounded-control border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
          >
            原文共 {{ contentData.length }} 字符，这里只显示前 {{ contentData.content.length }} 字符。
          </div>

          <div
            v-if="contentData.content.trim() === ''"
            class="rounded-control border border-line px-3 py-6 text-center text-sm text-slate-500"
          >
            这份文档解析后没有可显示的正文。
          </div>

          <!-- 用 pre 而不是渲染 Markdown：这段文字的价值是「原样」，渲染会引入第二套
               解释（标题层级、表格），与切块实际吃到的文本反而不一致。 -->
          <pre
            v-else
            class="max-h-[58vh] overflow-auto whitespace-pre-wrap break-words rounded-control border border-line bg-surface-muted p-4 font-mono text-[12.5px] leading-relaxed text-slate-700"
          >{{ contentData.content }}</pre>
        </template>
      </div>

      <template #footer>
        <el-button @click="contentOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <CreateKbDialog
      v-model="createOpen"
      @created="afterCreated"
      @continue-upload="continueUploadTo"
    />

    <!-- 版本时间轴：列表只读，回滚要过「会影响所有调用方」的确认（票 #177）。 -->
    <DocumentVersionsDialog
      v-model="versionsOpen"
      :document-id="versionsDocId"
      :document-title="versionsDocTitle"
      :document-status="versionsDocumentStatus"
      @rolled-back="afterRolledBack"
    />

    <UploadPickerDialog
      v-model="uploadOpen"
      :kb-options="kbOptions"
      :preset-kb-id="uploadPresetKbId"
      @upload="handleUpload"
    />

    <KbSettingsDialog :kb-id="settingsKbId" @updated="refresh" />

    <ConfirmDialog
      v-model="confirmOpen"
      :title="dialogTitle"
      :message="dialogMessage"
      :confirm-text="dialogConfirmText"
      :danger="dialogDanger"
      :loading="acting"
      :confirm-disabled="confirmDisabled"
      @confirm="confirmAction"
    />
  </section>
</template>
