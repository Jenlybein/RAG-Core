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
import CreateKbDialog from '../components/CreateKbDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import UploadPickerDialog from '../components/UploadPickerDialog.vue'
import {
  useUploadDocuments,
  type UploadFileItem,
  type UploadResultRow,
  type UploadRowStatus,
} from '../composables/useUploadDocuments'
import { useTreePolling } from '../composables/useTreePolling'
import { ApiError } from '../lib/envelope'
import {
  deleteDocumentConsole,
  deleteKbConsole,
  reindexDocumentConsole,
  uploadConsole,
} from '../services/consoleKb'
import { KNOWLEDGE_BASE_TREE_QUERY_KEY } from '../services/knowledgeBaseService'
import type { DocumentListItem } from '../types/document'
import type { KnowledgeBaseNode } from '../types/knowledgeBase'

const SPLIT_LABELS: Record<string, string> = {
  structure: '结构切块',
  size: '窗口切块',
}

const STATUS_TEXT: Record<UploadRowStatus, string> = {
  pending: '上传中…',
  success: '上传成功',
  // 回执里的 3 是「已受理、后台索引」，不是失败，更不是「0 个切片」。
  processing: '已提交，正在后台索引',
  failed: '上传失败',
  unknown: '未知状态',
}

function errorText(value: unknown): string {
  if (value instanceof ApiError) {
    // -1 是协议/连通性问题，没有业务码可对照；其余带上码，便于和后端日志对齐。
    return value.code === -1 ? value.message : `${value.message}（业务码 ${value.code}）`
  }
  if (value instanceof Error) return value.message
  return String(value)
}

/**
 * el-table 的插槽只把行标成 `DefaultRow`（不是我们的类型）。在边界处做一次形状判定，
 * 比在模板里堆类型断言可读得多——而且判定本身就是安全网：把库对象当文档发删除请求，
 * 在后端是一次 20016，在这里就是被拦下的一次空操作。
 */
function isKbRow(row: unknown): row is KnowledgeBaseNode {
  return typeof row === 'object' && row !== null && 'kb_id' in row && 'documents' in row
}

function isDocRow(row: unknown): row is DocumentListItem {
  return typeof row === 'object' && row !== null && 'document_id' in row
}

const router = useRouter()
const queryClient = useQueryClient()

const { treeQuery, hasProcessing } = useTreePolling()
const treePending = treeQuery.isPending
const treeError = treeQuery.error
const refetchTree = treeQuery.refetch
const tree = computed<KnowledgeBaseNode[]>(() => treeQuery.data.value ?? [])

/** 分块总数：详情与列表都要，按行求和放在一处，避免两个地方各算一遍。 */
function chunkTotal(documents: DocumentListItem[]): number {
  return documents.reduce((sum, doc) => sum + (doc.chunk_count ?? 0), 0)
}

// 搜索只在客户端过滤知识库名称：没有搜索参数的后端接口，前端不能自己发明一个 query。
const keyword = ref('')
const filteredTree = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (needle === '') return tree.value
  return tree.value.filter((kb) => kb.name.toLowerCase().includes(needle))
})

async function refresh(): Promise<void> {
  // 失效而不是手动 refetch：轮询、弹窗后的即时刷新走同一份缓存，避开两份状态互相对打。
  await queryClient.invalidateQueries({ queryKey: KNOWLEDGE_BASE_TREE_QUERY_KEY })
}

// ───────── 详情弹窗 ─────────
const detailKbId = ref<string | null>(null)
const detailOpen = ref(false)
const docKeyword = ref('')

/**
 * 详情认 `kb_id` 而不是把整个对象存下来：树会被轮询刷新，存对象会拿一份旧快照渲染
 * （刚传完的文档不出现，删除后还留着）。从当前树里按 id 取，永远是新的。
 */
const detailKb = computed<KnowledgeBaseNode | null>(() => {
  if (detailKbId.value === null) return null
  return tree.value.find((kb) => kb.kb_id === detailKbId.value) ?? null
})

const detailDocuments = computed<DocumentListItem[]>(() => {
  const kb = detailKb.value
  if (kb === null) return []
  const needle = docKeyword.value.trim().toLowerCase()
  if (needle === '') return kb.documents
  return kb.documents.filter((doc) => doc.title.toLowerCase().includes(needle))
})

function openDetail(row: unknown): void {
  if (!isKbRow(row)) return
  detailKbId.value = row.kb_id
  docKeyword.value = ''
  detailOpen.value = true
}

// ───────── 新建 / 上传弹窗 ─────────
const createOpen = ref(false)
const uploadOpen = ref(false)
const uploadPresetKbId = ref('')

const kbOptions = computed(() =>
  tree.value.map((kb) => ({
    value: kb.kb_id,
    label: `${kb.name}（${kb.documents.length} 篇）`,
  })),
)

function openUpload(kbId: string): void {
  uploadPresetKbId.value = kbId
  uploadOpen.value = true
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
  | { kind: 'deleteKb'; kb: KnowledgeBaseNode }
  | { kind: 'deleteDoc'; document: DocumentListItem }
  | { kind: 'reindex'; document: DocumentListItem }

const pendingAction = ref<PendingAction | null>(null)
const confirmOpen = ref(false)

const dialogTitle = computed(() => {
  const action = pendingAction.value
  if (action === null) return ''
  if (action.kind === 'deleteKb') return '删除知识库'
  if (action.kind === 'deleteDoc') return '删除文档'
  return action.document.status === 1 ? '重建索引（覆盖现有索引）' : '重试索引'
})

const dialogMessage = computed(() => {
  const action = pendingAction.value
  if (action === null) return ''
  if (action.kind === 'deleteKb') {
    const count = action.kb.documents.length
    if (count === 0) return `确认删除空知识库《${action.kb.name}》？删除后无法恢复。`
    // 非空库后端会拒绝；提前说清规则与做法，比让用户撞一次 20015 更好。
    return `《${action.kb.name}》里还有 ${count} 篇文档，删不掉。先在这个库的详情里把文档删空，再删库。`
  }
  if (action.kind === 'deleteDoc') {
    return `确认删除《${action.document.title}》？后端会清除切片、删除原文与记录，无法撤销。`
  }
  // 成功的文档已有可用索引：force=true 会丢掉它，语义要说清。
  if (action.document.status === 1) {
    return `《${action.document.title}》已经有可用索引。重建会用新的切块结果覆盖它（旧索引会被丢弃），确认继续？`
  }
  // 重建回执恒为 status=3：说清楚「只是重新排队」，别让用户以为点完就恢复成功。
  return `重新提交《${action.document.title}》的索引任务？提交后状态回到「索引中」，由后台重新处理。`
})

const dialogConfirmText = computed(() => {
  const action = pendingAction.value
  if (action === null) return '确认'
  if (action.kind === 'reindex') return action.document.status === 1 ? '覆盖重建' : '重试'
  return '删除'
})

const dialogDanger = computed(() => pendingAction.value?.kind !== 'reindex')
/** 非空库不允许点删除确认：规则来自后端，这里只是别给一个注定失败的按钮。 */
const confirmDisabled = computed(
  () => pendingAction.value?.kind === 'deleteKb' && pendingAction.value.kb.documents.length > 0,
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
  mutationFn: (kb: KnowledgeBaseNode) => deleteKbConsole(kb.kb_id),
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
  mutationFn: (document: DocumentListItem) => deleteDocumentConsole(document.document_id),
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
  mutationFn: (document: DocumentListItem) =>
    // 已成功的文档必须显式 force：后端否则回 20027，替用户保住还能用的索引。
    reindexDocumentConsole(document.document_id, { force: document.status === 1 }),
  onSuccess: () => {
    ElMessage.success('已重新提交索引，稍后自动刷新')
    closeDialog()
    void refresh()
  },
  onError: (error) => {
    ElMessage.error(errorText(error))
  },
})

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
  else reindexMutation.mutate(action.document)
}

function goRetrieve(kbId: string): void {
  void router.push({ path: '/retrieve', query: { kb_id: kbId } })
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader
      title="知识库管理"
      subtitle="一个页面管完：建库、往库里传文档、看库里有什么、删库。点任意一行看详情；上传与索引都是异步的，有任务在跑时每 3 秒自动刷新。"
    >
      <template #actions>
        <el-button type="primary" @click="createOpen = true">新建知识库</el-button>
      </template>
    </PageHeader>

    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <span class="th-label">共 {{ tree.length }} 个知识库</span>
          <el-input v-model="keyword" class="w-64" placeholder="搜索知识库名称" clearable>
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>
      </template>

      <!-- 处理中提示：只在确实有 status=3 时出现，不是常驻横幅。 -->
      <div
        v-if="hasProcessing"
        class="mb-3 flex items-center gap-2 rounded-control border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
      >
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>有文档正在后台索引：页面每 3 秒自动刷新，全部完成后停止。</span>
      </div>

      <p v-if="treePending" class="py-6 text-center text-sm text-slate-500">正在读取知识库…</p>

      <!-- 读取失败不假装空列表：空列表会让人以为库被删了。 -->
      <div
        v-else-if="treeError !== null"
        class="rounded-control border border-red-200 bg-red-50 px-3 py-3 text-sm text-red-700"
      >
        <p class="m-0">{{ errorText(treeError) }}</p>
        <el-button class="mt-2" size="small" @click="refetchTree()">重试</el-button>
      </div>

      <div v-else-if="tree.length === 0" class="py-10 text-center">
        <p class="text-[15px] font-semibold text-ink">还没有知识库</p>
        <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted">
          文档必须放进一个知识库才能被检索。先建一个：切块策略决定它收哪些格式，建好后可以直接把文档传进去。
        </p>
        <el-button type="primary" class="!mt-5" @click="createOpen = true">新建知识库</el-button>
      </div>

      <div v-else-if="filteredTree.length === 0" class="py-8 text-center text-sm text-slate-500">
        没有名称匹配「{{ keyword.trim() }}」的知识库。
      </div>

      <el-table
        v-else
        :data="filteredTree"
        row-key="kb_id"
        class="w-full cursor-pointer"
        @row-click="openDetail"
      >
        <el-table-column label="知识库" min-width="240">
          <template #default="{ row }">
            <span class="text-sm font-medium text-ink">{{ row.name }}</span>
            <p class="mt-0.5 text-xs text-muted">{{ row.allowed_formats.join('、') || '（无格式）' }}</p>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <!-- 原文照 show：structure / size 是请求里的取值，同时给中文标签，免得用户猜 -->
            <span class="text-sm text-slate-700">{{ SPLIT_LABELS[row.split_strategy] ?? row.split_strategy }}</span>
            <p class="mt-0.5 text-[11px] text-muted">{{ row.split_strategy }}</p>
          </template>
        </el-table-column>

        <el-table-column label="文档数 / 分块数" width="150">
          <template #default="{ row }">
            <span class="text-sm text-slate-700">
              {{ row.documents.length }} 篇 / {{ chunkTotal(row.documents) }} 块
            </span>
          </template>
        </el-table-column>

        <el-table-column label="分块窗口" width="120">
          <template #default="{ row }">
            <span class="text-sm text-slate-700">{{ row.chunk_size }} token</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="240">
          <template #default="{ row }">
            <!-- el-button 的点击要阻止冒泡，否则会顺带触发整行的「详情」 -->
            <div class="flex flex-wrap items-center gap-1" @click.stop>
              <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
              <el-button link type="primary" size="small" @click="openUpload(row.kb_id)">上传</el-button>
              <el-button link type="primary" size="small" @click="goRetrieve(row.kb_id)">检索</el-button>
              <el-button link type="danger" size="small" @click="askDeleteKb(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 本次上传回执：只在真传过之后出现 -->
    <el-card v-if="showResults" shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 class="section-title">本次上传</h2>
            <p class="section-hint">
              status=3 表示后端已受理、正在后台索引，不是失败；切片数要等索引结束后才可查。
            </p>
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

    <!-- ───────── 详情：库内文档的增删查改 ───────── -->
    <el-dialog v-model="detailOpen" width="760px" :close-on-click-modal="false">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-3 pr-6">
          <div>
            <h2 class="text-[17px] font-semibold tracking-tight text-ink">
              {{ detailKb?.name ?? '知识库详情' }}
            </h2>
            <p v-if="detailKb !== null" class="mt-0.5 text-xs text-muted">
              {{ SPLIT_LABELS[detailKb.split_strategy] ?? detailKb.split_strategy }} ·
              {{ detailKb.documents.length }} 篇 /
              {{ chunkTotal(detailKb.documents) }} 块 · 窗口 {{ detailKb.chunk_size }} token
            </p>
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
        <el-input v-model="docKeyword" placeholder="搜索文档标题" clearable>
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <p v-if="detailKb === null" class="py-6 text-center text-sm text-slate-500">
          这个知识库已不在列表里（可能刚被删除）。
        </p>

        <p
          v-else-if="detailKb.documents.length === 0"
          class="rounded-control border border-line px-3 py-6 text-center text-sm text-slate-500"
        >
          这个库还没有文档。点右上角「上传文档」把语料放进来。
        </p>

        <p
          v-else-if="detailDocuments.length === 0"
          class="py-4 text-center text-sm text-slate-500"
        >
          没有标题匹配「{{ docKeyword.trim() }}」的文档。
        </p>

        <el-table v-else :data="detailDocuments" row-key="document_id" class="w-full">
          <el-table-column label="文档" min-width="240">
            <template #default="{ row }">
              <span class="break-all text-sm text-ink">{{ row.title }}</span>
              <p class="mt-0.5 text-[11px] text-muted">{{ row.source_type }}</p>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <StatusBadge :status="row.status" :error-message="row.error_message" />
            </template>
          </el-table-column>

          <el-table-column label="分块数" width="90">
            <template #default="{ row }">
              <span class="text-sm text-slate-700">{{ row.chunk_count }} 块</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <div class="flex flex-wrap items-center gap-1">
                <!-- 处理中没有任何写操作：此刻重建会回 20025、删除会回 20025，按钮只是陷阱。 -->
                <span v-if="row.status === 3" class="text-xs text-slate-400">处理中，无操作</span>
                <template v-else-if="row.status === 1 || row.status === 2">
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
      </div>

      <template #footer>
        <el-button @click="detailOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <CreateKbDialog
      v-model="createOpen"
      @created="afterCreated"
      @continue-upload="continueUploadTo"
    />

    <UploadPickerDialog
      v-model="uploadOpen"
      :kb-options="kbOptions"
      :preset-kb-id="uploadPresetKbId"
      @upload="handleUpload"
    />

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
