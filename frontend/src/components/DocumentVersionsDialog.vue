<script setup lang="ts">
/**
 * 一篇文档的**版本列表 + 回滚**（调试台，`GET/POST /console/documents/{id}/versions...`）。
 *
 * 为什么单独一个组件而不是塞进 `KnowledgeManagePage`：那个页面已经背着「建库 / 上传 /
 * 删除 / 重建 / 读原文」五件事，再叠一层版本时间轴会把「哪个状态属于哪一层」彻底搅乱。
 * 版本是**文档级、可回溯**的东西，给它一个自己的弹窗，页面只留一个入口按钮。
 *
 * 三条刻意的设计：
 *
 * - **回滚必须先确认，而且明说影响面**。面板上一次点击改的不是「我看到的这一页」，
 *   而是整个租户所有调用方检索到的内容；确认框里那句「会影响所有调用方」是票面验收项，
 *   不是为了好看。确认按钮用危险色：这是一次对线上检索结果的替换。
 * - **当前稿不给回滚按钮**。后端会回 20032（已经是当前稿），提前把按钮拿掉比让用户撞
 *   一次错误更省事；行里仍然显示「当前」标记，因为「哪一稿现在是生效的」正是要看的。
 * - **列表不拉正文**。正文可能是整篇文档（上传上限 20MB），列表只需要稿号 / 状态 /
 *   校验和 / 字符数 / 时间；`char_count` 就是为「不拉正文也能看出哪一稿很长」而存在的。
 *
 * 数据来源与写回走 `services/consoleKb.ts`（控制面会话门），本组件不 import
 * `lib/dataHttp`——调试台的身份是浏览器 cookie，不是 API Key。
 */
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { errorText } from '../lib/errorText'
import {
  fetchDocumentVersionsConsole,
  rollbackDocumentVersionConsole,
} from '../services/consoleKb'
import type { DocumentVersionItem, DocumentVersionList } from '../types/document'
import ConfirmDialog from './ConfirmDialog.vue'

const open = defineModel<boolean>({ required: true })

const props = defineProps<{
  /** 要看哪篇文档；为 null 时弹窗里是空态（页面还没选中文档）。 */
  documentId: string | null
  /** 标题（文档 title）：弹窗头直接显示，便于确认自己点的是哪一篇。 */
  documentTitle: string
  /**
   * 文档此刻正在索引（`status=3`）：回滚会被后端拒绝（20025，切稿会让「哪一稿的切片」
   * 变成掷骰子）。这时不给按钮，也不假装能点——「能点但必然失败」比没有按钮更糟。
   */
  busy?: boolean
}>()

const emit = defineEmits<{
  /** 回滚受理后通知页面刷新树：进度与终态都要靠轮询。 */
  rolledBack: [version: number]
}>()

const versions = ref<DocumentVersionList | null>(null)
const loading = ref(false)
const loadError = ref('')

const items = computed<DocumentVersionItem[]>(() => versions.value?.items ?? [])
const currentVersion = computed(() => versions.value?.current_version ?? null)

async function load(): Promise<void> {
  const documentId = props.documentId
  if (documentId === null) {
    versions.value = null
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    versions.value = await fetchDocumentVersionsConsole(documentId)
  } catch (error) {
    // 20016（跨租户 / 已删）也走这里：把后端原话给出来，比一句「加载失败」有用。
    loadError.value = errorText(error)
    versions.value = null
  } finally {
    loading.value = false
  }
}

// 打开时读一次；同一个弹窗换文档（连着点两行的「版本」）也重新读一次。
watch(
  () => [open.value, props.documentId] as const,
  ([isOpen]) => {
    if (isOpen) void load()
  },
  { immediate: true },
)

/** 时间按本地时区显示成「YYYY-MM-DD HH:mm」；解析不了就原样回显，不编造一个时间。 */
function formatTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  const pad = (part: number): string => String(part).padStart(2, '0')
  return (
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
  )
}

/** 校验和只回显前 8 位：它是用来「两稿是不是同一份正文」的，不需要整行 64 位。 */
function shortChecksum(value: string): string {
  return value.length > 8 ? `${value.slice(0, 8)}…` : value
}

const confirmOpen = ref(false)
const pendingVersion = ref<number | null>(null)
const rollingBack = ref(false)

const confirmTitle = computed(() =>
  pendingVersion.value === null ? '回滚版本' : `回滚到第 ${pendingVersion.value} 稿`,
)

/**
 * 确认框正文。**必须说清影响面**：回滚改的是这个租户所有调用方检索到的内容，
 * 而不是某个页面上的显示；同时纠正一个常见误解——「点完立刻能查到旧内容」是错的，
 * 切片要重建。
 */
const confirmMessage = computed(() => {
  const target = pendingVersion.value
  if (target === null || versions.value === null) return ''
  const from = versions.value.current_version
  return (
    `回滚会把当前稿从第 ${from} 稿切到第 ${target} 稿，并且会影响所有调用方：` +
    `整个租户的检索立刻改为命中第 ${target} 稿的内容。\n` +
    '回滚不产生新稿次，但会投递一次重建；在重建完成之前，这篇文档可能暂时检索不到。'
  )
})

function askRollback(item: DocumentVersionItem): void {
  pendingVersion.value = item.version
  confirmOpen.value = true
}

function closeConfirm(): void {
  confirmOpen.value = false
  pendingVersion.value = null
}

async function confirmRollback(): Promise<void> {
  const documentId = props.documentId
  const target = pendingVersion.value
  if (documentId === null || target === null) return
  rollingBack.value = true
  try {
    const receipt = await rollbackDocumentVersionConsole(documentId, target)
    ElMessage.success(`已回滚到第 ${receipt.version} 稿，正在后台重建索引`)
    closeConfirm()
    emit('rolledBack', receipt.version)
    await load()
  } catch (error) {
    // 20025 / 20030 / 20032 都是业务结果：把后端原话显示出来，别翻译成「回滚失败」。
    ElMessage.error(errorText(error))
    // 被拒往往意味着库已经变了（这一稿被保留策略删掉 / 别人刚切了稿）：重读一次列表，
    // 别让用户对着已经过期的行再点一次。
    await load()
  } finally {
    rollingBack.value = false
  }
}
</script>

<template>
  <el-dialog v-model="open" append-to-body width="760px" :close-on-click-modal="false">
    <template #header>
      <div class="pr-6">
        <h2 class="text-[17px] font-semibold tracking-tight text-ink">
          {{ documentTitle }} · 版本
        </h2>
        <p class="mt-0.5 text-xs text-muted">
          <template v-if="currentVersion !== null">
            当前稿是第 {{ currentVersion }} 稿 · 共 {{ items.length }} 稿
          </template>
          <template v-else>按稿号倒序，现在这一稿在最前</template>
        </p>
      </div>
    </template>

    <div class="space-y-3">
      <p v-if="loading" class="py-8 text-center text-sm text-muted">正在读取版本…</p>

      <div
        v-else-if="loadError !== ''"
        class="rounded-control border border-red-200 bg-red-50 px-3 py-3 text-sm text-red-700"
      >
        <p class="m-0">{{ loadError }}</p>
        <el-button class="mt-2" size="small" @click="load()">重试</el-button>
      </div>

      <p
        v-else-if="items.length === 0"
        class="rounded-control border border-line px-3 py-6 text-center text-sm text-slate-500"
      >
        这篇文档还没有版本行（索引成功之后才会有第一稿）。
      </p>

      <table v-else class="w-full border-collapse text-xs">
        <thead class="bg-surface-muted">
          <tr>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">
              稿号
            </th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">
              状态
            </th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">
              字符数
            </th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">
              校验和
            </th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">
              时间
            </th>
            <th class="border-b border-line px-2 py-1.5 text-left font-medium text-slate-600">
              操作
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.version" class="align-top">
            <td class="border-b border-line px-2 py-1.5 font-mono text-ink">
              v{{ item.version }}
            </td>
            <td class="border-b border-line px-2 py-1.5">
              <span v-if="item.status === 'current'" class="chip">当前</span>
              <span v-else class="text-slate-500">归档</span>
            </td>
            <td class="border-b border-line px-2 py-1.5 text-slate-700">
              {{ item.char_count }} 字符
            </td>
            <td
              class="border-b border-line px-2 py-1.5 font-mono text-slate-500"
              :title="item.checksum"
            >
              {{ shortChecksum(item.checksum) }}
            </td>
            <td class="border-b border-line px-2 py-1.5 text-slate-700">
              {{ formatTime(item.created_at) }}
            </td>
            <td class="border-b border-line px-2 py-1.5">
              <!-- 当前稿不给回滚入口：后端会回 20032（已经是当前稿），按钮只是陷阱。 -->
              <span v-if="item.status === 'current'" class="text-xs text-muted">—</span>
              <!-- 文档正在索引时同样不给：后端会回 20025。 -->
              <span v-else-if="busy === true" class="text-xs text-muted">索引中</span>
              <el-button v-else link type="primary" size="small" @click="askRollback(item)">
                回滚到此稿
              </el-button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <template #footer>
      <el-button @click="open = false">关闭</el-button>
    </template>
  </el-dialog>

  <ConfirmDialog
    v-model="confirmOpen"
    :title="confirmTitle"
    :message="confirmMessage"
    confirm-text="确认回滚"
    danger
    :loading="rollingBack"
    @confirm="confirmRollback"
  />
</template>
