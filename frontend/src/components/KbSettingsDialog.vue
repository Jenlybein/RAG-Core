<script setup lang="ts">
/**
 * 改知识库的切块设置（第 16 步 #207）。
 *
 * 这个弹窗的重点不是表单，而是**说清代价**：切块参数是索引期旋钮，改完必须整库重建
 * （全库重嵌入 + 重写 ES）。所以打开时先读一次 `/settings/preview`，把「这次会重建
 * 多少篇、多少片」摆在确认按钮旁边——让人对着一个「确定要重建吗？」点确定，等于把
 * 几分钟到几十分钟的 Worker 变成盲盒。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { errorText } from '../lib/errorText'
import { fetchKbSettingsPreview, updateKbSettingsConsole } from '../services/consoleKb'
import {
  KB_CHUNK_SIZE_MAX,
  KB_CHUNK_SIZE_MIN,
  KB_SECTION_PART_MAX,
  KB_SECTION_PART_MIN,
  type KbSettingsPreview,
  type KbSplitStrategy,
} from '../types/knowledgeBase'

const props = defineProps<{
  /** 打开时传库 id；`null` 表示关闭。 */
  kbId: string | null
}>()

const emit = defineEmits<{ (event: 'updated', kbId: string): void }>()

const open = ref(false)
const preview = ref<KbSettingsPreview | null>(null)
const loading = ref(false)
const saving = ref(false)
const confirming = ref(false)
const errors = ref<string[]>([])

const form = ref({
  splitStrategy: 'structure' as KbSplitStrategy,
  chunkSize: 512,
  sectionPartMaxChars: 2000,
})

/** 改了什么：只有真的变了才值得写一行「将重建」，否则一次打开就显示几十篇很吓人。 */
const changed = computed(() => {
  const current = preview.value
  if (current === null) return false
  return (
    form.value.splitStrategy !== current.split_strategy ||
    form.value.chunkSize !== current.chunk_size ||
    form.value.sectionPartMaxChars !== current.section_part_max_chars
  )
})

const buildRunning = computed(() => preview.value?.build_running ?? false)

watch(
  () => props.kbId,
  async (kbId) => {
    if (kbId === null) {
      open.value = false
      return
    }
    open.value = true
    confirming.value = false
    errors.value = []
    await load(kbId)
  },
)

async function load(kbId: string): Promise<void> {
  loading.value = true
  try {
    const data = await fetchKbSettingsPreview(kbId)
    preview.value = data
    form.value = {
      splitStrategy: data.split_strategy as KbSplitStrategy,
      chunkSize: data.chunk_size,
      sectionPartMaxChars: data.section_part_max_chars,
    }
  } catch (error) {
    errors.value = [errorText(error)]
  } finally {
    loading.value = false
  }
}

function requestConfirm(): void {
  errors.value = []
  if (buildRunning.value) {
    errors.value = ['这个库正在重建，等它结束再改设置']
    return
  }
  if (!changed.value) {
    errors.value = ['设置没有变化']
    return
  }
  confirming.value = true
}

async function submit(): Promise<void> {
  const kbId = props.kbId
  if (kbId === null) return
  saving.value = true
  errors.value = []
  try {
    const receipt = await updateKbSettingsConsole(kbId, {
      split_strategy: form.value.splitStrategy,
      chunk_size: form.value.chunkSize,
      section_part_max_chars: form.value.sectionPartMaxChars,
      // 唯一的确认就是在上面那个 `confirming` 分支里点的这一下。
      confirm: true,
    })
    ElMessage.success(`已保存，重建已排队（版本 ${receipt.build_version}）`)
    open.value = false
    emit('updated', kbId)
  } catch (error) {
    errors.value = [errorText(error)]
  } finally {
    saving.value = false
  }
}

/** `structure` 只收 Markdown；换成 `size` 才能收别的格式——这条规则来自后端，这里只提示。 */
const strategyHint = computed(() =>
  form.value.splitStrategy === 'structure'
    ? 'structure 按 Markdown 标题结构切；库只收 Markdown。'
    : 'size 按窗口大小切，不认标题与表格；五种格式都能收。',
)
</script>

<template>
  <el-dialog v-model="open" append-to-body title="切块设置" width="620px" :close-on-click-modal="false">
    <div v-if="loading" class="py-6 text-center text-sm text-muted">正在读取设置…</div>

    <el-form v-else label-position="top" @submit.prevent="requestConfirm">
      <el-form-item label="切块策略">
        <el-radio-group v-model="form.splitStrategy" :disabled="confirming">
          <el-radio value="structure">structure</el-radio>
          <el-radio value="size">size</el-radio>
        </el-radio-group>
        <p class="mt-1 text-xs text-muted">{{ strategyHint }}</p>
      </el-form-item>

      <el-form-item label="切块窗口（token）">
        <el-input-number
          v-model="form.chunkSize"
          :min="KB_CHUNK_SIZE_MIN"
          :max="KB_CHUNK_SIZE_MAX"
          :step="64"
          controls-position="right"
          :disabled="confirming"
        />
        <p class="mt-1 text-xs text-muted">
          默认 512 token ≈ 768–983 个汉字。structure 策略下它只是**上限**，改小可能看不出变化。
        </p>
      </el-form-item>

      <el-form-item label="节段上限（字）">
        <el-input-number
          v-model="form.sectionPartMaxChars"
          :min="KB_SECTION_PART_MIN"
          :max="KB_SECTION_PART_MAX"
          :step="500"
          controls-position="right"
          :disabled="confirming"
        />
        <p class="mt-1 text-xs text-muted">
          一个 h2 超过这个字数就在片边界切成若干「节段」。它是「一条结果不会上万字」的根，
          默认 2000。
        </p>
      </el-form-item>

      <!--
        生效版本参数：让人能看到「现在线上用的是哪套参数」。老版本没留参数时显式说
        「没留参数」，而不是显示成默认值——那会让人以为它一直是这样。
      -->
      <div class="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
        <p>
          生效版本：
          <span class="font-medium">{{ preview?.live_version ?? '（无）' }}</span>
          <span v-if="preview?.live_params === null"> · 这一版没留切块参数</span>
          <span v-else-if="preview?.live_params">
            · 策略 {{ preview.live_params.split_strategy }} · 窗口
            {{ preview.live_params.chunk_size }} · 节段上限
            {{ preview.live_params.section_part_max_chars }}
          </span>
        </p>
        <p v-if="preview?.needs_rebuild" class="mt-1 text-amber-700">
          当前设置与这个版本不一致，需要重建后才生效。
        </p>
      </div>
    </el-form>

    <!-- 第二步：确认。这里才出现「会重建多少」——第一步不该被这堆数字淹没。 -->
    <div v-if="confirming" class="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3">
      <p class="text-sm font-semibold text-amber-800">改设置会触发整库重建</p>
      <p class="mt-1 text-xs leading-relaxed text-amber-700">
        将重嵌入 {{ preview?.documents_to_rebuild ?? 0 }} 篇文档、重写
        {{ preview?.chunks_to_rebuild ?? 0 }} 个切片；期间旧索引继续服务，完成后自动切换。
        构建失败时旧版本照旧生效，设置会留着（不回滚）。
      </p>
    </div>

    <p v-for="message in errors" :key="message" class="mt-2 text-xs text-rose-600">
      {{ message }}
    </p>

    <template #footer>
      <div class="flex justify-end gap-2">
        <el-button @click="open = false">取消</el-button>
        <el-button v-if="confirming" :loading="saving" type="danger" @click="submit">
          确认并重建
        </el-button>
        <el-button v-else type="primary" @click="requestConfirm">保存</el-button>
      </div>
    </template>
  </el-dialog>
</template>
