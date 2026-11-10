<script setup lang="ts">
/**
 * 上传页：建库 → 自动回填 kb_id → 批量上传 → 逐行异步回执。
 *
 * 只走数据面（Bearer API Key）：不做列表页、检索页、登录或任何 /admin。
 * 结果行只用文案表达状态，不在这里写「status === 1 用什么颜色」——状态徽章是后续
 * 统一组件的事，本票提前分色会让两个地方的映射慢慢漂移。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { z } from 'zod'

import {
  useUploadDocuments,
  type UploadFileItem,
  type UploadResultRow,
  type UploadRowStatus,
} from '../composables/useUploadDocuments'
import { ApiError } from '../lib/envelope'
import { uploadDocumentFile } from '../services/documentService'
import { createKnowledgeBase } from '../services/knowledgeBaseService'
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

    const kb = await createKnowledgeBase(payload)
    created.value = kb
    // 建库的终点是「不用复制粘贴」：同一个动作里把 ID 交给上传表单。
    kbId.value = kb.kb_id
    ElMessage.success(`知识库已创建：${kb.kb_id}`)
  } catch (error) {
    formErrors.value = [errorText(error)]
  } finally {
    creating.value = false
  }
}

// —— 上传表单 ——
const kbId = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
const pendingFiles = ref<UploadFileItem[]>([])
const filteredCount = ref(0)

const uploader = useUploadDocuments({
  upload: (item, targetKbId) =>
    // 标题用文件基名：选文件夹时 item.fileName 含相对路径，整条路径当文档标题只是噪声。
    uploadDocumentFile({ kb_id: targetKbId, title: item.file.name, file: item.file }),
})
const { results, running, succeededCount, failedCount, uploadAll } = uploader

function applyKbIdQuery(value: unknown): void {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string') return
  const trimmed = raw.trim()
  if (trimmed === '') return
  kbId.value = trimmed
}

// 初次读一次：带着 ?kb_id= 刷新/直达时，上传表单应该已经是填好的。
applyKbIdQuery(route.query.kb_id)
// 同一路由只改 query 不会重新挂载组件（比如从别处跳回 /?kb_id=...），
// 只读一次会让输入框停在上一个库；这一步是文档里点名的坑。
watch(
  () => route.query.kb_id,
  (value) => applyKbIdQuery(value),
)

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
    ElMessage.warning('先填知识库 ID：建库后会自动回填，也可以粘贴一个已有库的 ID')
    return
  }
  if (pendingFiles.value.length === 0) {
    ElMessage.warning('先选择要上传的文件或文件夹')
    return
  }
  // 编排在 composable 里：这里只管「什么时候开始」，不在这里写循环与失败隔离。
  await uploadAll(targetKbId, pendingFiles.value)
}

function rowErrorText(row: UploadResultRow): string {
  if (row.errorMessage === null) return ''
  if (row.errorCode === null || row.errorCode === -1) return row.errorMessage
  return `${row.errorMessage}（业务码 ${row.errorCode}）`
}
</script>

<template>
  <section class="space-y-4">
    <div class="grid gap-4 lg:grid-cols-2">
      <!-- 1. 创建知识库 -->
      <el-card shadow="never">
        <template #header>
          <div>
            <h2 class="text-sm font-semibold text-slate-800">创建知识库</h2>
            <p class="mt-1 text-xs text-slate-500">
              切块策略同时决定这个库收哪些格式；建好后 ID 会自动带到右侧上传表单。
            </p>
          </div>
        </template>

        <el-form label-position="top" @submit.prevent="submitCreate">
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
            <p class="mt-1 text-xs text-slate-500">
              structure 的全部能力都建立在 Markdown 标题语法上；size 不依赖结构，五种格式都能收。
            </p>
          </el-form-item>

          <el-form-item label="语料格式">
            <div v-if="isStructure">
              <el-checkbox :model-value="true" disabled>
                Markdown（.md / .markdown）
              </el-checkbox>
              <p class="mt-1 text-xs text-slate-500">
                这是语料准入而不是偏好：structure 只收 Markdown，收别的只会退化成窗口硬切。
              </p>
            </div>
            <div v-else>
              <el-checkbox-group v-model="sizeFormats">
                <el-checkbox v-for="fmt in KB_FORMATS" :key="fmt" :value="fmt">
                  {{ FORMAT_LABELS[fmt] }}
                </el-checkbox>
              </el-checkbox-group>
              <p class="mt-1 text-xs text-slate-500">
                默认五种全开；可以按这个库的语料收窄，至少保留一种。
              </p>
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
                <p class="mt-1 text-xs text-slate-500">
                  默认 {{ KB_CHUNK_SIZE_DEFAULT }} token ≈ 768–983 个汉字，范围
                  {{ KB_CHUNK_SIZE_MIN }}–{{ KB_CHUNK_SIZE_MAX }}。
                </p>
              </el-form-item>
            </el-collapse-item>
          </el-collapse>

          <div
            v-if="formErrors.length > 0"
            class="mb-3 mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            <p v-for="message in formErrors" :key="message">{{ message }}</p>
          </div>

          <div class="mt-4 flex items-center gap-3">
            <el-button type="primary" native-type="submit" :loading="creating">
              创建知识库
            </el-button>
            <span class="text-xs text-slate-400">成功后 kb_id 自动回填到右侧</span>
          </div>
        </el-form>

        <div v-if="created !== null" class="mt-4 rounded border border-line bg-slate-50 p-3">
          <p class="text-xs text-slate-500">新建知识库 ID（只读）</p>
          <el-input :model-value="created.kb_id" readonly class="mt-2" />
          <p class="mt-2 break-words text-xs text-slate-500">
            已自动填入右侧上传表单；这个库接收：{{
              created.allowed_formats.join('、') || '（无）'
            }}
          </p>
        </div>
      </el-card>

      <!-- 2. 上传文档 -->
      <el-card shadow="never">
        <template #header>
          <div>
            <h2 class="text-sm font-semibold text-slate-800">上传文档</h2>
            <p class="mt-1 text-xs text-slate-500">
              串行上传：一份回执回来后再发下一份，单个失败不会中断后面的文件。
            </p>
          </div>
        </template>

        <div class="space-y-4">
          <div>
            <label class="mb-1 block text-sm text-slate-700">知识库 ID</label>
            <el-input
              v-model="kbId"
              placeholder="建库后自动回填，也可粘贴已有库的 kb_id"
              clearable
            />
          </div>

          <div>
            <label class="mb-1 block text-sm text-slate-700">选择语料</label>
            <div class="flex flex-wrap items-center gap-2">
              <el-button @click="fileInput?.click()">选择文件</el-button>
              <el-button @click="folderInput?.click()">选择文件夹</el-button>
              <span class="text-xs text-slate-400">支持 .md / .markdown / .txt / .docx / .pdf</span>
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
            class="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
          >
            已过滤 {{ filteredCount }} 个文件（隐藏文件，或扩展名不在支持列表）
          </div>

          <div class="rounded border border-line">
            <div class="flex items-center justify-between border-b border-line px-3 py-2">
              <span class="text-sm font-medium text-slate-700">待上传清单</span>
              <span class="text-xs text-slate-500">共 {{ pendingFiles.length }} 个</span>
            </div>
            <ul v-if="pendingFiles.length > 0" class="max-h-48 divide-y divide-line overflow-auto">
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

          <div class="flex items-center gap-3">
            <el-button
              type="primary"
              :disabled="running || pendingFiles.length === 0 || kbId.trim() === ''"
              :loading="running"
              @click="startUpload"
            >
              开始上传
            </el-button>
            <span class="text-xs text-slate-400">串行执行，不并发</span>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 3. 上传结果 -->
    <el-card shadow="never">
      <template #header>
        <div class="flex items-center justify-between gap-4">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">上传结果</h2>
            <p class="mt-1 text-xs text-slate-500">
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

      <p v-if="results.length === 0" class="text-sm text-slate-400">暂无上传结果</p>
      <ul v-else class="divide-y divide-line">
        <li v-for="row in results" :key="row.key" class="py-2.5">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span class="break-all text-sm font-medium text-slate-800">{{ row.fileName }}</span>
            <span class="text-sm text-slate-600">{{ STATUS_TEXT[row.status] }}</span>
          </div>
          <div class="mt-1 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
            <span class="break-all">document_id：{{ row.documentId ?? '—' }}</span>
            <span>chunk_count：{{ row.chunkCount === null ? '—' : row.chunkCount }}</span>
          </div>
          <p
            v-if="row.errorMessage !== null"
            class="mt-1 break-words text-xs text-danger"
          >
            {{ rowErrorText(row) }}
          </p>
        </li>
      </ul>
    </el-card>
  </section>
</template>
