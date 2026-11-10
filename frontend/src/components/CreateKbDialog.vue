<script setup lang="ts">
/**
 * 「新建知识库」弹窗。
 *
 * 抽成组件而不是留在页面里：建库表单有跨字段规则（切块策略同时决定收哪些格式），
 * 把它和列表/详情的模板混在一个文件里，改一条校验规则就得在几百行模板中间找。
 * 弹窗只负责「填与提交」，成功与否由父组件通过 `created` 事件接管（刷新列表等）。
 *
 * 建库成功不直接关窗：就地切成「下一步」面板，让用户能一键继续上传到这个新库。
 * 否则他得记住（或去别处复制）那个 UUID 才能传文档——这是这套界面刻意要消灭的动作。
 */
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { z } from 'zod'

import { ApiError } from '../lib/envelope'
import { createKbConsole } from '../services/consoleKb'
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

/** 格式名是后端的 `FormatKind`；给用户看的始终是「文件类型 + 常见扩展名」。 */
const FORMAT_LABELS: Record<KbFormat, string> = {
  markdown: 'Markdown（.md / .markdown）',
  text: '纯文本（.txt）',
  docx: 'Word（.docx）',
  pdf: 'PDF',
  html: 'HTML',
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  /** 建库成功；父组件据此刷新列表。 */
  created: [kb: CreatedKnowledgeBase]
  /** 用户选择「继续上传」；父组件拿这个 id 打开上传弹窗。 */
  continueUpload: [kbId: string]
}>()

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

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

function reset(): void {
  form.name = ''
  form.description = ''
  form.splitStrategy = 'structure'
  form.chunkSize = KB_CHUNK_SIZE_DEFAULT
  sizeFormats.value = [...KB_FORMATS]
  advancedPanels.value = []
  formErrors.value = []
  created.value = null
}

/** 每次打开都从干净状态开始：上一次的报错与回执带进新的一次创建只会误导。 */
watch(open, (value) => {
  if (value) reset()
})

async function submit(): Promise<void> {
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
    ElMessage.success(`知识库已创建：${created.value.name}`)
    emit('created', created.value)
  } catch (error) {
    formErrors.value = [errorText(error)]
  } finally {
    creating.value = false
  }
}

function continueUpload(): void {
  const kb = created.value
  if (kb === null) return
  open.value = false
  emit('continueUpload', kb.kb_id)
}
</script>

<template>
  <el-dialog
    v-model="open"
    title="新建知识库"
    width="560px"
    :close-on-click-modal="false"
  >
    <!-- 成功后的下一步：不要求用户去别处复制那个 ID -->
    <div v-if="created !== null" class="py-2">
      <p class="text-[15px] font-semibold text-ink">已创建：{{ created.name }}</p>
      <p class="mt-2 break-words text-sm leading-relaxed text-muted">
        这个库接收：{{ created.allowed_formats.join('、') || '（无）' }}。下一步可以直接把文档传进去。
      </p>
    </div>

    <el-form v-else label-position="top" @submit.prevent="submit">
      <el-form-item label="名称（必填）">
        <el-input v-model="form.name" maxlength="255" show-word-limit placeholder="例如：产品手册" />
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
          <el-checkbox :model-value="true" disabled>Markdown（.md / .markdown）</el-checkbox>
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
        <el-button @click="open = false">完成</el-button>
        <el-button type="primary" @click="continueUpload">继续上传文档</el-button>
      </template>
      <template v-else>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submit">创建</el-button>
      </template>
    </template>
  </el-dialog>
</template>
