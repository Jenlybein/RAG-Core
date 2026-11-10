<script setup lang="ts">
/**
 * 知识库列表页：知识库（父）→ 文档（子）两级树 + 三态状态 + 有处理中才轮询。
 *
 * 只走数据面（Bearer API Key）：本票是阶段 1，不切控制面会话，也不做知识库编辑
 * （数据面没有改库接口）。父行的「去上传 / 去检索」把 `kb_id` 带进 query，
 * 两个目标页只读 URL，不依赖任何跨页全局状态。
 *
 * 状态色只由 `StatusBadge` 决定；本页只决定「按状态给哪些操作」——那是业务规则，
 * 与「成功/失败长什么样」是两件事。
 */
import { Loading, Search } from '@element-plus/icons-vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import ConfirmDialog from '../components/ConfirmDialog.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { useTreePolling } from '../composables/useTreePolling'
import { ApiError } from '../lib/envelope'
import { deleteDocument, reindexDocument } from '../services/documentService'
import { KNOWLEDGE_BASE_TREE_QUERY_KEY } from '../services/knowledgeBaseService'
import type { DocumentListItem } from '../types/document'
import type { KnowledgeBaseNode } from '../types/knowledgeBase'

/**
 * 父行有 `documents`，子行没有；用它区分，而不是靠下标或额外的标记字段。
 * 参数收 `unknown` 是因为 el-table 的插槽只把行标成 `DefaultRow`（不是我们的树类型）：
 * 在边界处做一次形状判定，比在模板里堆类型断言可读得多。
 */
function isKnowledgeBaseRow(row: unknown): row is KnowledgeBaseNode {
  return typeof row === 'object' && row !== null && 'documents' in row
}

/** 子行必须是文档项；判定同时排除父行，避免把库对象当文档发删除请求。 */
function isDocumentRow(row: unknown): row is DocumentListItem {
  return typeof row === 'object' && row !== null && 'document_id' in row
}

function errorText(value: unknown): string {
  if (value instanceof ApiError) {
    // -1 是协议/连通性问题，没有业务码可对照；其余带上码，便于和后端日志对齐。
    return value.code === -1 ? value.message : `${value.message}（业务码 ${value.code}）`
  }
  if (value instanceof Error) return value.message
  return String(value)
}

const router = useRouter()
const queryClient = useQueryClient()

const { treeQuery, hasProcessing } = useTreePolling()
// 解构出 ref 再交给模板：模板只对其顶层的 ref 自动解包，`treeQuery.isPending` 不会。
const treePending = treeQuery.isPending
const treeError = treeQuery.error
const refetchTree = treeQuery.refetch

const tree = computed<KnowledgeBaseNode[]>(() => treeQuery.data.value ?? [])

// 搜索只在客户端过滤知识库名称：数据面没有搜索参数，前端不能自己发明一个「后端不认识」的 query。
const keyword = ref('')
const filteredTree = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (needle === '') return tree.value
  return tree.value.filter((knowledgeBase) => knowledgeBase.name.toLowerCase().includes(needle))
})

async function refreshTree(): Promise<void> {
  // 失效而不是手动 refetch：轮询、弹窗后的即时刷新都走同一份缓存，避开两份状态互相对打。
  await queryClient.invalidateQueries({ queryKey: KNOWLEDGE_BASE_TREE_QUERY_KEY })
}

/**
 * 待确认的动作。确认弹窗只抛 `confirm`，具体发哪个请求由这里决定：
 * 删除与「重试索引」都是不可撤销动作，共用同一个受控弹窗。
 */
type PendingAction =
  | { kind: 'delete'; document: DocumentListItem }
  | { kind: 'reindex'; document: DocumentListItem }

const pendingAction = ref<PendingAction | null>(null)
const confirmOpen = ref(false)

const dialogTitle = computed(() =>
  pendingAction.value?.kind === 'delete' ? '删除文档' : '重试索引',
)
const dialogMessage = computed(() => {
  const action = pendingAction.value
  if (action === null) return ''
  if (action.kind === 'delete') {
    return `确认删除《${action.document.title}》？后端会清除切片、删除原文与记录，无法撤销。`
  }
  // 重建回执恒为 status=3：说清楚「只是重新排队」，别让用户以为点完就恢复成功。
  return `重新提交《${action.document.title}》的索引任务？提交后状态回到「索引中」，由后台重新处理。`
})
const dialogConfirmText = computed(() =>
  pendingAction.value?.kind === 'delete' ? '删除' : '重试',
)
const dialogDanger = computed(() => pendingAction.value?.kind === 'delete')

function askDelete(row: unknown): void {
  if (!isDocumentRow(row)) return
  pendingAction.value = { kind: 'delete', document: row }
  confirmOpen.value = true
}

function askRetry(row: unknown): void {
  if (!isDocumentRow(row)) return
  pendingAction.value = { kind: 'reindex', document: row }
  confirmOpen.value = true
}

function closeDialog(): void {
  confirmOpen.value = false
  pendingAction.value = null
}

// 「重试」只出现在失败文档上：它们本来就没有可用索引，不需要 force；
// 成功文档的重建需要 force=true（否则后端回 20027），但验收要求成功行只有「删除」，
// 因此这里不提供那条入口，force 的语义留在 service 层，等有入口时再传。
const deleteMutation = useMutation({
  mutationFn: (document: DocumentListItem) => deleteDocument(document.document_id),
  onSuccess: () => {
    ElMessage.success('文档已删除，列表已刷新')
    closeDialog()
    void refreshTree()
  },
  onError: (error) => {
    // 失败时保留弹窗：用户多半想换个动作或再试一次；错误文案已经说明原因。
    ElMessage.error(errorText(error))
  },
})

const reindexMutation = useMutation({
  mutationFn: (document: DocumentListItem) =>
    reindexDocument(document.document_id, { force: false }),
  onSuccess: () => {
    // 回执 status=3 只代表已排队投递；行状态要等下一次树查询变成「索引中」。
    ElMessage.success('已重新提交索引，稍后自动刷新')
    closeDialog()
    void refreshTree()
  },
  onError: (error) => {
    ElMessage.error(errorText(error))
  },
})

const acting = computed(
  () => deleteMutation.isPending.value || reindexMutation.isPending.value,
)

function confirmAction(): void {
  const action = pendingAction.value
  if (action === null) return
  if (action.kind === 'delete') deleteMutation.mutate(action.document)
  else reindexMutation.mutate(action.document)
}

function goUpload(row: unknown): void {
  if (!isKnowledgeBaseRow(row)) return
  void router.push({ path: '/', query: { kb_id: row.kb_id } })
}

function goRetrieve(row: unknown): void {
  if (!isKnowledgeBaseRow(row)) return
  void router.push({ path: '/retrieve', query: { kb_id: row.kb_id } })
}
</script>

<template>
  <section class="space-y-4">
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">知识库与文档</h2>
            <p class="mt-1 text-xs text-slate-500">
              展开知识库查看其文档；有文档在索引时每 3 秒自动刷新，全部进入终态后停止。
            </p>
          </div>
          <el-input
            v-model="keyword"
            class="w-64"
            placeholder="搜索知识库名称"
            clearable
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>
      </template>

      <!-- 处理中提示：只在确实有 status=3 时出现，不是常驻横幅。 -->
      <div
        v-if="hasProcessing"
        class="mb-3 flex items-center gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
      >
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>有文档正在后台索引：页面每 3 秒自动刷新，全部完成后停止。</span>
      </div>

      <p v-if="treePending" class="py-6 text-center text-sm text-slate-500">
        正在读取知识库与文档…
      </p>

      <!-- 读取失败不假装空列表：空列表会让人以为库被删了。 -->
      <div
        v-else-if="treeError !== null"
        class="rounded border border-red-200 bg-red-50 px-3 py-3 text-sm text-red-700"
      >
        <p class="m-0">{{ errorText(treeError) }}</p>
        <el-button class="mt-2" size="small" @click="refetchTree()">重试</el-button>
      </div>

      <div
        v-else-if="filteredTree.length === 0"
        class="py-8 text-center text-sm text-slate-500"
      >
        <p v-if="keyword.trim() !== ''">没有名称匹配「{{ keyword.trim() }}」的知识库。</p>
        <p v-else>
          还没有知识库，或当前 Key 的密钥配置没有授权任何库。
          <RouterLink to="/" class="text-brand hover:underline">去上传页建一个</RouterLink>
        </p>
      </div>

      <el-table
        v-else
        :data="filteredTree"
        row-key="kb_id"
        :tree-props="{ children: 'documents' }"
        default-expand-all
        class="w-full"
      >
        <el-table-column label="知识库 / 文档" min-width="260">
          <template #default="{ row }">
            <span
              v-if="isKnowledgeBaseRow(row)"
              class="text-sm font-medium text-slate-800"
            >
              {{ row.name }}
            </span>
            <span v-else class="break-all text-sm text-slate-700">{{ row.title }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <!-- 父行没有单文档状态，用「—」而不是硬编一个假的「成功」。 -->
            <span v-if="isKnowledgeBaseRow(row)" class="text-sm text-slate-400">—</span>
            <StatusBadge v-else :status="row.status" :error-message="row.error_message" />
          </template>
        </el-table-column>

        <el-table-column label="文档数 / 分块数" width="150">
          <template #default="{ row }">
            <span v-if="isKnowledgeBaseRow(row)" class="text-sm text-slate-600">
              {{ row.documents.length }} 篇
            </span>
            <span v-else class="text-sm text-slate-600">{{ row.chunk_count }} 块</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" min-width="200">
          <template #default="{ row }">
            <div v-if="isKnowledgeBaseRow(row)" class="flex flex-wrap gap-3 text-sm">
              <el-button link type="primary" size="small" @click="goUpload(row)">
                去上传
              </el-button>
              <el-button link type="primary" size="small" @click="goRetrieve(row)">
                去检索
              </el-button>
            </div>
            <div v-else class="flex flex-wrap items-center gap-3 text-sm">
              <!-- 处理中没有任何操作：此刻重建会回 20025、删除会回 20025，按钮只是陷阱。 -->
              <span v-if="row.status === 3" class="text-xs text-slate-400">
                处理中，无操作
              </span>
              <template v-else-if="row.status === 2">
                <el-button link type="primary" size="small" @click="askRetry(row)">
                  重试
                </el-button>
                <el-button link type="danger" size="small" @click="askDelete(row)">
                  删除
                </el-button>
              </template>
              <template v-else-if="row.status === 1">
                <el-button link type="danger" size="small" @click="askDelete(row)">
                  删除
                </el-button>
              </template>
              <!-- 未知状态不提供任何写操作：后端将来新增状态时，宁可少一个入口也别乱删。 -->
              <span v-else class="text-xs text-slate-400">—</span>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <ConfirmDialog
      v-model="confirmOpen"
      :title="dialogTitle"
      :message="dialogMessage"
      :confirm-text="dialogConfirmText"
      :danger="dialogDanger"
      :loading="acting"
      @confirm="confirmAction"
    />
  </section>
</template>
