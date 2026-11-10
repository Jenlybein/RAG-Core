<script setup lang="ts">
/**
 * 「上传文档」弹窗：**只负责选**（选库 + 选文件），上传交给调用方。
 *
 * 为什么拆成这样：上传进度与逐行回执属于页面（那是结果，不该被一个关掉的弹窗带走），
 * 而「选哪些文件」是弹窗自己的临时状态。分开之后，页面在弹窗 `upload` 事件里拿一批
 * 文件就能开跑，不必让弹窗持有业务的编排。
 *
 * 库是从下拉里选的，不是手填 ID：UUID 不是人能记住的东西。
 */
import { computed, ref, watch } from 'vue'

import type { UploadFileItem } from '../composables/useUploadDocuments'
import { isSupportedUploadFile } from '../utils/file'

export interface KbOption {
  value: string
  label: string
}

const props = defineProps<{
  modelValue: boolean
  /** 可选的知识库清单。 */
  kbOptions: KbOption[]
  /** 预选的库（来自「上传到这个库」或刚建好的库）。 */
  presetKbId?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  upload: [kbId: string, files: UploadFileItem[]]
}>()

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const kbId = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
const pendingFiles = ref<UploadFileItem[]>([])
const filteredCount = ref(0)

/** 预选的库优先；没预选且只有一个库时直接选上（省掉一次没有信息量的点击）。 */
function syncSelection(): void {
  const preset = props.presetKbId?.trim() ?? ''
  if (preset !== '') {
    kbId.value = preset
    return
  }
  if (kbId.value.trim() === '' && props.kbOptions.length === 1) {
    kbId.value = props.kbOptions[0].value
  }
}

/**
 * 选项里要包含「预选但不在清单中」的库：深链带来一个已被删掉的 kb_id 时，
 * 下拉若不含它就会显示空值，用户以为没选——其实他选的是一个不存在的库。
 */
const options = computed<KbOption[]>(() => {
  const list = [...props.kbOptions]
  const current = kbId.value.trim()
  if (current !== '' && !list.some((option) => option.value === current)) {
    list.push({ value: current, label: `${current}（不在清单中）` })
  }
  return list
})

// 每次打开都重置「这一批文件」：留着会让下一次上传悄悄带上上一批的选择。
watch(open, (value) => {
  if (!value) {
    pendingFiles.value = []
    filteredCount.value = 0
    return
  }
  filteredCount.value = 0
  pendingFiles.value = []
  syncSelection()
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

const canSubmit = computed(() => kbId.value.trim() !== '' && pendingFiles.value.length > 0)

function start(): void {
  if (!canSubmit.value) return
  const target = kbId.value.trim()
  const batch = [...pendingFiles.value]
  open.value = false
  emit('upload', target, batch)
}

/** 目录选择是浏览器扩展属性，类型里没有；用原生 API 打上，不为此引依赖。 */
function bindFolderInput(element: unknown): void {
  if (element instanceof HTMLInputElement) element.setAttribute('webkitdirectory', '')
}
</script>

<template>
  <el-dialog
    v-model="open"
    title="上传文档"
    width="640px"
    :close-on-click-modal="false"
  >
    <div class="space-y-5">
      <div>
        <label class="mb-1.5 block text-[12px] font-medium text-slate-600">上传到哪个知识库</label>
        <el-select v-model="kbId" filterable placeholder="选一个知识库" class="w-full">
          <el-option v-for="option in options" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <p class="mt-1.5 text-xs text-muted">
          文档必须落在一个已存在的知识库里；清单里没有的库不可以传。
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
          :ref="(el) => bindFolderInput(el)"
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
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :disabled="!canSubmit" @click="start">
        开始上传（{{ pendingFiles.length }} 个）
      </el-button>
    </template>
  </el-dialog>
</template>
