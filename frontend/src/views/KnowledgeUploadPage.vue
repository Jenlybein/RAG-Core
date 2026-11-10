<script setup lang="ts">
/**
 * 知识库页（原「上传文档」页）：建库与上传都以**弹窗**形式发起，页面本身只呈现结果。
 *
 * 为什么改成弹窗（这页最容易被做错的地方）：
 *
 * - 创建与上传是**偶发动作**，不是常驻工作区。两张常驻大表单把页面变成一张考卷，
 *   用户每次进来都要先读懂一堆字段才能找到「其实我只想传个文件」。
 * - 上传原来要**手填知识库 ID（UUID）**。UUID 不是人能记住的东西，让用户复制粘贴
 *   一个 36 位串才能干活，是这页最不直觉的一处。现在改成「从清单里选库」；
 *   清单里也没有的才需要粘贴（作为回退，仍保留）。
 * - 建库完成后不再要求「把 ID 抄到右边」。对话框直接给出下一步：继续上传到这个新库。
 *
 * 两条写路径都走**控制面会话门**（`services/consoleKb.ts`）：建库与上传按会话租户
 * 归属，不再用构建期 dev-key。数据面只剩「拿选中 Key 打真实检索」一件事（#92 的
 * 身份收口），所以本页不 import `lib/dataHttp`。
 *
 * 结果行只用文案表达状态，不在这里写「status === 1 用什么颜色」——状态徽章由
 * `StatusBadge` 统一映射。
 */
import { useQuery } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { z } from 'zod'

import PageHeader from '../components/PageHeader.vue'
import {
  useUploadDocuments,
  type UploadFileItem,
  type UploadResultRow,
  type UploadRowStatus,
} from '../composables/useUploadDocuments'
import { ApiError } from '../lib/envelope'
import { createKbConsole, uploadConsole } from '../services/consoleKb'
import {
  KNOWLEDGE_BASE_TREE_QUERY_KEY,
  fetchTree,
} from '../services/knowledgeBaseService'
import {
  KB_CHUNK_SIZE_DEFAULT,
  KB_CHUNK_SIZE_MAX,
  KB_CHUNK_SIZE_MIN,
  KB_FORMATS,
  KB_SPLIT_STRATEGIES,
  type CreatedKnowledgeBase,
  type CreateKnowledgeBaseRequest,
  type KbFormat,
  type KbSplitStrategy,
  type KnowledgeBaseNode,
} from '../types/knowledgeBase'
import { isSupportedUploadFile } from '../utils/file'

/** 格式名是后端的 `FormatKind`；给用户看的始终是「文件类型 + 常见扩展名」。 */
const FORMAT_LABELS: Record<KbFormat, string> = {
  markdown: 'Markdown（.md / .markdown）',
  text: '纯文本（.txt）',
  docx: 'Word（.docx）',
  pdf: 'PDF',
  html: 'HTML',
}

const STATUS_TEXT: Record<UploadRowStatus, string> = {
  pending: '上传中…',
  success: '上传成功',
  // 回执里的 3 是「已受理、后台索引」，不是失败，更不是「0 个切片」。
  processing: '已提交，正在后台索引',
  failed: '上传失败',
  unknown: '未知状态',
}

const route = useRoute()
const router = useRouter()

// —— 库清单：既是页面主体，也是上传弹窗里的「选库」数据源 ——
// 与列表页共用同一个 query key：从列表页跳过来时命中缓存，不会白闪一次。
const treeQuery = useQuery({
  queryKey: KNOWLEDGE_BASE_TREE_QUERY_KEY,
  queryFn: fetchTree,
})
const kbList = computed<KnowledgeBaseNode[]>(() => treeQuery.data.value ?? [])
const kbLoading = computed(() => treeQuery.isPending.value)

// —— 弹窗开关 ——
const createOpen = ref(false)
const uploadOpen = ref(false)

// —— 建库表单 ——
const form = reactive({
  name: '',
  description: '',
  splitStrategy: 'structure' as KbSplitStrategy,
  chunkSize: KB_CHUNK_SIZE_DEFAULT,
})
// size 策略下用户的可勾选名单；structure 时不可见，也不参与请求。
const sizeFormats = ref<KbFormat[]>([...KB_FORMATS])
const advancedPanels = ref<string[]>([])
const formErrors = ref<string[]>([])
const creating = ref(false)
/** 建库成功后 Dialog 切成「下一步」面板，不让用户去别处找那个 ID。 */
const created = ref<CreatedKnowledgeBase | null>(null)

const isStructure = computed(() => form.splitStrategy === 'structure')
const selectedFormats = computed<KbFormat[]>(() =>
  // structure 的 [markdown] 是语料准入，不是偏好：用户没有取消它的入口。
  isStructure.value ? ['markdown'] : sizeFormats.value,
)

/**
 * 表单校验用 zod，而不是散在模板里的 required 标记：名称、格式清单与切块大小
 * 有跨字段关系（structure 只能 markdown），集中一个 schema 才能一次说清所有错误。
 */
const createFormSchema = z.object({
  name: z.string().trim().min(1, '知识库名称必填').max(255, '名称最多 255 个字符'),
  description: z.string().trim(),
  split_strategy: z.enum(KB_SPLIT_STRATEGIES),
  allowed_formats: z.array(z.enum(KB_FORMATS)).min(1, '至少保留一种格式'),
  chunk_size: z
    .number()
    .int('切块大小必须是整数')
    .min(KB_CHUNK_SIZE_MIN, `切块大小不能小于 ${KB_CHUNK_SIZE_MIN}`)
    .max(KB_CHUNK_SIZE_MAX, `切块大小不能大于 ${KB_CHUNK_SIZE_MAX}`),
})

// structure 期间勾选框被隐藏；切回 size 时恢复五种全开，否则用户带着一份自己
// 看不见的缩窄名单去建库，行为看起来像「默认没全开」。
watch(
  () => form.splitStrategy,
  (strategy) => {
    if (strategy === 'size') sizeFormats.value = [...KB_FORMATS]
  },
)

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    // code=-1 是协议/连通性问题，没有业务码可报；其余把码带上，方便对照文档。
    return error.code === -1 ? error.message : `${error.message}（业务码 ${error.code}）`
  }
  return error instanceof Error ? error.message : String(error)
}

function openCreate(): void {
  // 每次打开都从干净状态开始：上一次的报错与成功回执带到新的一次创建里只会误导。
  created.value = null
  formErrors.value = []
  createOpen.value = true
}

async function submitCreate(): Promise<void> {
  const parsed = createFormSchema.safeParse({
    name: form.name,
    description: form.description,
    split_strategy: form.splitStrategy,
    allowed_formats: selectedFormats.value,
    chunk_size: form.chunkSize,
  })
  if (!parsed.success) {
    formErrors.value = parsed.error.issues.map((issue) => issue.message)
    return
  }
  formErrors.value = []
  creating.value = true
  try {
    const payload: CreateKnowledgeBaseRequest = {
      name: parsed.data.name,
      split_strategy: parsed.data.split_strategy,
      allowed_formats: parsed.data.allowed_formats,
      chunk_size: parsed.data.chunk_size,
    }
    // 后端 description 可选；空串按「没填」发，别把一个无意义的空字段塞进 extra=forbid 的请求。
    if (parsed.data.description !== '') payload.description = parsed.data.description

    created.value = await createKbConsole(payload)
    // 新库要立刻出现在清单与「选库」里，否则用户刚建完却选不到它。
    void treeQuery.refetch()
  } catch (error) {
    formErrors.value = [errorText(error)]
  } finally {
    creating.value = false
  }
}

/** 建库成功后的「继续上传」：把刚建的库带进上传弹窗，用户不必认识那个 ID。 */
function continueToUpload(): void {
  const kb = created.value
  if (kb === null) return
  createOpen.value = false
  resetCreateForm()
  openUpload(kb.kb_id)
}

function resetCreateForm(): void {
  form.name = ''
  form.description = ''
  form.splitStrategy = 'structure'
  form.chunkSize = KB_CHUNK_SIZE_DEFAULT
  sizeFormats.value = [...KB_FORMATS]
  advancedPanels.value = []
  formErrors.value = []
  created.value = null
}

// —— 上传表单 ——
const kbId = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
const pendingFiles = ref<UploadFileItem[]>([])
const filteredCount = ref(0)

/** 选库下拉的选项；`?kb_id=` 带来一个清单里没有的库时，补一條以免用户看不到自己选的。 */
const kbOptions = computed(() => {
  const options = kbList.value.map((kb) => ({
    value: kb.kb_id,
    label: `${kb.name}（${kb.documents.length} 篇）`,
  }))
  const current = kbId.value.trim()
  if (current !== '' && !options.some((option) => option.value === current)) {
    options.push({ value: current, label: `${current}（不在清单中）` })
  }
  return options
})

const uploader = useUploadDocuments({
  upload: (item, targetKbId) =>
    // 标题用文件基名：选文件夹时 item.fileName 含相对路径，整条路径当文档标题只是噪声。
    uploadConsole({ kb_id: targetKbId, title: item.file.name, file: item.file }),
})
const { results, running, succeededCount, failedCount, uploadAll } = uploader

/**
 * 从 query 取 `kb_id`，返回「这次是否真的带来了一个库」。
 *
 * 返回值必须是布尔而不是「读到了就赋值」：关闭弹窗时我们会把 `?kb_id=` 从 URL 里清掉，
 * 那一次 watcher 收到的就是空值。若在这种情况下也去开窗，用户点「取消」的下一秒弹窗
 * 又自己回来了——看起来像关不掉。
 */
function applyKbIdQuery(value: unknown): boolean {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string') return false
  const trimmed = raw.trim()
  if (trimmed === '') return false
  kbId.value = trimmed
  return true
}

// 带着 ?kb_id= 直达（列表页「去上传」就是这么跳的）时，直接弹出上传窗、库已选好：
// 用户点的是「上传到这个库」，落地后不该再让他自己找一遍库。
if (applyKbIdQuery(route.query.kb_id)) uploadOpen.value = true

// 同一路由只改 query 不会重新挂载组件（比如从别处跳回 /?kb_id=...），
// 只读一次会让输入框停在上一个库；这一步是文档里点名的坑。
watch(
  () => route.query.kb_id,
  (value) => {
    if (applyKbIdQuery(value)) uploadOpen.value = true
  },
)

/** 打开上传弹窗；`presetKbId` 用于「上传到这个库」与建库后的下一步。 */
function openUpload(presetKbId?: string): void {
  if (presetKbId !== undefined) kbId.value = presetKbId
  // 清单里只有一个库时直接选上：这不是替用户做决定，而是省掉一次没有信息量的点击。
  if (kbId.value.trim() === '' && kbList.value.length === 1) {
    kbId.value = kbList.value[0].kb_id
  }
  filteredCount.value = 0
  uploadOpen.value = true
}

/** 关闭上传窗时清掉「这批文件」：留着会让下一次上传悄悄带上上一批的选择。 */
function closeUpload(): void {
  pendingFiles.value = []
  filteredCount.value = 0
  uploadOpen.value = false
  // 弹窗是「上传到某个库」的入口，关掉后不该在 URL 里留一个会再次自动弹出的参数。
  if (route.query.kb_id !== undefined) void router.replace({ path: '/', query: {} })
}

onMounted(() => {
  // HTMLInputElement 的类型里没有 webkitdirectory，写在模板会被 vue-tsc 当成未知属性；
  // 目录选择是浏览器扩展属性，用原生 API 打上，不为此引入依赖。
  folderInput.value?.setAttribute('webkitdirectory', '')
})

function fileNameOf(file: File): string {
  // 选文件夹时相对路径才能区分不同子目录下的同名文件。
  return file.webkitRelativePath || file.name
}

function onPickFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  const picked = Array.from(input.files ?? [])
  const supported = picked.filter(isSupportedUploadFile)
  filteredCount.value = picked.length - supported.length
  // 每次选择替换整批而不是追加：选错目录时重新选一次就能撤回，不必逐个删。
  pendingFiles.value = supported.map((file) => ({ file, fileName: fileNameOf(file) }))
  // 同一批文件再选一次不会触发 change，除非先清空 value。
  input.value = ''
}

async function startUpload(): Promise<void> {
  const targetKbId = kbId.value.trim()
  if (targetKbId === '') {
    ElMessage.warning('先选一个知识库')
    return
  }
  if (pendingFiles.value.length === 0) {
    ElMessage.warning('先选择要上传的文件或文件夹')
    return
  }
  // **先把这一批文件抄下来再关窗**：closeUpload() 会清空 pendingFiles（下一次开窗不该
  // 带着上一批），若在关窗之后再读 pendingFiles.value，读到的就是空数组——症状是
  // 「点了开始上传，弹窗关了，什么都没发生」。
  const batch = [...pendingFiles.value]
  // 弹窗在开跑后立刻关掉：进度与逐行回执在页面主体里看得更全，
  // 也不必让用户守着一个挡住结果的对话框。
  closeUpload()
  // 编排在 composable 里：这里只管「什么时候开始」，不在这里写循环与失败隔离。
  await uploadAll(targetKbId, batch)
  void treeQuery.refetch()
}

/** 建库弹窗关闭后清掉上次的报错；成功态由「完成」或「继续上传」显式清。 */
function onCreateClosed(): void {
  if (created.value === null) formErrors.value = []
}

function rowErrorText(row: UploadResultRow): string {
  if (row.errorMessage === null) return ''
  if (row.errorCode === null || row.errorCode === -1) return row.errorMessage
  return `${row.errorMessage}（业务码 ${row.errorCode}）`
}

const showResults = computed(() => results.value.length > 0)
</script>

<template>
  <section class="space-y-6">
    <PageHeader
      title="建库与上传"
      subtitle="建库与上传都是偶发动作，所以收在按钮后面；页面本身只放「有哪些库」和「这一轮传了什么」。上传是异步的：提交成功即返回，索引在后台跑。"
    >
      <template #actions>
        <el-button :disabled="kbLoading" @click="openUpload()">上传文档</el-button>
        <el-button type="primary" @click="openCreate">新建知识库</el-button>
      </template>
    </PageHeader>

    <!-- 空态：没有任何库时，与其列一个空的表格，不如只说下一步做什么 -->
    <div v-if="!kbLoading && kbList.length === 0" class="card card-body text-center">
      <p class="text-[15px] font-semibold text-ink">还没有知识库</p>
      <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted">
        文档必须放进一个知识库才能被检索。先建一个：切块策略决定它收哪些格式，建好后可以直接把文档传进去。
      </p>
      <el-button type="primary" class="!mt-5" @click="openCreate">新建知识库</el-button>
    </div>

    <!-- 库清单：这不是表单，只是「有哪些库、各自多少文档」，每行都可以直接传文档 -->
    <el-card v-else shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="th-label">共 {{ kbList.length }} 个知识库</span>
          <RouterLink to="/knowledge-bases" class="text-[13px] text-slate-600 hover:text-ink">
            看文档与索引状态 →
          </RouterLink>
        </div>
      </template>

      <p v-if="kbLoading" class="text-sm text-slate-400">正在读取知识库…</p>
      <ul v-else class="divide-y divide-line">
        <li
          v-for="kb in kbList"
          :key="kb.kb_id"
          class="flex flex-wrap items-center justify-between gap-3 py-3"
        >
          <div class="min-w-0">
            <p class="truncate text-sm font-medium text-ink">{{ kb.name }}</p>
            <p class="mt-1 text-xs text-muted">
              {{ kb.split_strategy }} · 收 {{ kb.allowed_formats.join('、') || '（无）' }} ·
              {{ kb.documents.length }} 篇文档 · 窗口 {{ kb.chunk_size }} token
            </p>
          </div>
          <el-button size="small" @click="openUpload(kb.kb_id)">上传到这个库</el-button>
        </li>
      </ul>
    </el-card>

    <!-- 上传结果：只在真的传过之后出现。空着摆一张「暂无上传结果」的卡片只是噪声 -->
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

    <!-- ───────── 新建知识库 ───────── -->
    <el-dialog
      v-model="createOpen"
      title="新建知识库"
      width="560px"
      :close-on-click-modal="false"
      @closed="onCreateClosed"
    >
      <!-- 建库成功后的下一步：直接把人送到「传文档」，不让他去别处复制那个 ID -->
      <div v-if="created !== null" class="py-2">
        <p class="text-[15px] font-semibold text-ink">已创建：{{ created.name }}</p>
        <p class="mt-2 break-words text-sm leading-relaxed text-muted">
          这个库接收：{{ created.allowed_formats.join('、') || '（无）' }}。下一步可以直接把文档传进去。
        </p>
      </div>

      <el-form v-else label-position="top" @submit.prevent="submitCreate">
        <el-form-item label="名称（必填）">
          <el-input
            v-model="form.name"
            maxlength="255"
            show-word-limit
            placeholder="例如：产品手册"
          />
        </el-form-item>

        <el-form-item label="描述（可选）">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="这个库存什么语料"
          />
        </el-form-item>

        <el-form-item label="切块策略（必选，二选一）">
          <el-radio-group v-model="form.splitStrategy">
            <el-radio value="structure">structure：按标题结构切</el-radio>
            <el-radio value="size">size：按窗口大小切</el-radio>
          </el-radio-group>
          <p class="mt-1 text-xs text-muted">
            structure 的全部能力都建立在 Markdown 标题语法上；size 不依赖结构，五种格式都能收。
          </p>
        </el-form-item>

        <el-form-item label="语料格式">
          <div v-if="isStructure">
            <el-checkbox :model-value="true" disabled>
              Markdown（.md / .markdown）
            </el-checkbox>
            <p class="mt-1 text-xs text-muted">
              这是语料准入而不是偏好：structure 只收 Markdown，收别的只会退化成窗口硬切。
            </p>
          </div>
          <div v-else>
            <el-checkbox-group v-model="sizeFormats">
              <el-checkbox v-for="fmt in KB_FORMATS" :key="fmt" :value="fmt">
                {{ FORMAT_LABELS[fmt] }}
              </el-checkbox>
            </el-checkbox-group>
            <p class="mt-1 text-xs text-muted">默认五种全开；可以按这个库的语料收窄，至少保留一种。</p>
          </div>
        </el-form-item>

        <el-collapse v-model="advancedPanels">
          <el-collapse-item title="高级" name="advanced">
            <el-form-item label="切块大小（token）">
              <el-input-number
                v-model="form.chunkSize"
                :min="KB_CHUNK_SIZE_MIN"
                :max="KB_CHUNK_SIZE_MAX"
                :step="64"
              />
              <p class="mt-1 text-xs text-muted">
                默认 {{ KB_CHUNK_SIZE_DEFAULT }} token ≈ 768–983 个汉字，范围
                {{ KB_CHUNK_SIZE_MIN }}–{{ KB_CHUNK_SIZE_MAX }}。
              </p>
            </el-form-item>
          </el-collapse-item>
        </el-collapse>

        <!-- 错误就近显示在表单里，不出现在弹窗外的页面顶部 -->
        <div
          v-if="formErrors.length > 0"
          class="mt-4 rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
        >
          <p v-for="message in formErrors" :key="message">{{ message }}</p>
        </div>
      </el-form>

      <template #footer>
        <template v-if="created !== null">
          <el-button @click="createOpen = false; resetCreateForm()">完成</el-button>
          <el-button type="primary" @click="continueToUpload">继续上传文档</el-button>
        </template>
        <template v-else>
          <el-button @click="createOpen = false">取消</el-button>
          <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
        </template>
      </template>
    </el-dialog>

    <!-- ───────── 上传文档 ───────── -->
    <el-dialog
      v-model="uploadOpen"
      title="上传文档"
      width="640px"
      :close-on-click-modal="false"
      @close="closeUpload"
    >
      <div class="space-y-5">
        <div>
          <label class="mb-1.5 block text-[12px] font-medium text-slate-600">上传到哪个知识库</label>
          <el-select v-model="kbId" filterable placeholder="选一个知识库" class="w-full">
            <el-option
              v-for="option in kbOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <p class="mt-1.5 text-xs text-muted">
            清单里没有的库不可以传：文档必须落在一个已存在的知识库里。
            <RouterLink to="/knowledge-bases" class="text-slate-600 underline-offset-2 hover:underline">
              去看全部库
            </RouterLink>
          </p>
        </div>

        <div>
          <label class="mb-1.5 block text-[12px] font-medium text-slate-600">选择语料</label>
          <div class="flex flex-wrap items-center gap-2">
            <el-button @click="fileInput?.click()">选择文件</el-button>
            <el-button @click="folderInput?.click()">选择文件夹</el-button>
            <span class="text-xs text-muted">支持 .md / .markdown / .txt / .docx / .pdf</span>
          </div>
          <input
            ref="fileInput"
            class="hidden"
            type="file"
            multiple
            accept=".md,.markdown,.txt,.docx,.pdf"
            @change="onPickFiles"
          />
          <input
            ref="folderInput"
            class="hidden"
            type="file"
            multiple
            accept=".md,.markdown,.txt,.docx,.pdf"
            @change="onPickFiles"
          />
        </div>

        <div
          v-if="filteredCount > 0"
          class="rounded-control border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
        >
          已过滤 {{ filteredCount }} 个文件（隐藏文件，或扩展名不在支持列表）
        </div>

        <div class="rounded-control border border-line">
          <div class="flex items-center justify-between border-b border-line px-3 py-2">
            <span class="text-sm font-medium text-slate-700">待上传清单</span>
            <span class="text-xs text-muted">共 {{ pendingFiles.length }} 个</span>
          </div>
          <ul v-if="pendingFiles.length > 0" class="max-h-52 divide-y divide-line overflow-auto">
            <li
              v-for="(item, index) in pendingFiles"
              :key="`${index}-${item.fileName}`"
              class="px-3 py-1.5 text-sm text-slate-700"
            >
              {{ item.fileName }}
            </li>
          </ul>
          <p v-else class="px-3 py-4 text-sm text-slate-400">
            还没选择文件。选文件夹时隐藏文件会被过滤掉。
          </p>
        </div>
      </div>

      <template #footer>
        <el-button @click="closeUpload">取消</el-button>
        <el-button
          type="primary"
          :disabled="running || pendingFiles.length === 0 || kbId.trim() === ''"
          :loading="running"
          @click="startUpload"
        >
          开始上传（{{ pendingFiles.length }} 个）
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
