<script setup lang="ts">
/**
 * 「获取 Key」的两态弹窗：**警示态 → 展示态**，是明文在这一页唯一出现过的地方。
 *
 * 为什么把两种状态放进同一个组件，而不是「警示一个框 + 结果一个框」：签发与重新生成
 * 的明文都只出现这一次，用户点「确认」之前看到的承诺（关掉就取不回来）必须由展示态原样
 * 兑现。两个独立弹窗可以在中间被关掉、被别的弹窗顶掉，于是「确认过的那句承诺」和
 * 「现在这一屏」就对不上了；一个组件从警示切到展示，是同一段流程没有断点。
 *
 * 为什么展示态**不能点遮罩 / ESC 关闭**：明文只在这一屏里存在。误触关闭意味着用户还没
 * 复制就永久失去了它（库里只有 scrypt 哈希），唯一的补救是再轮换一次——而轮换会作废
 * 刚给出去的那把。所以关掉这件事只能由「关闭」按钮明确表达。
 *
 * 明文本身**不进本组件**：`plaintext` 是父组件的 prop，关闭时由父组件清掉；组件内部的
 * 任何状态（勾选、已复制）都在关闭时复位。这样「关掉后组件里还留着明文」在结构上就不成立。
 */
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const open = defineModel<boolean>({ required: true })

const props = defineProps<{
  /**
   * `regenerate` = 重新生成（当前 Key 会进 24 小时过渡期，警示里要交代代价，需要勾选确认）；
   * `issue` = 签发一把新的（不影响任何旧 Key，所以既没有过渡期文案，也不需要勾选）。
   */
  mode: 'regenerate' | 'issue'
  /** 展示态的明文；`null` 表示还在警示态（或已关闭）。关掉即由父组件置回 `null`。 */
  plaintext: string | null
  /** 请求在途：锁住警示态的确认按钮，避免连点发出两次轮换。 */
  loading?: boolean
}>()

const emit = defineEmits<{ cancel: []; confirm: []; debug: [] }>()

/** 展示态 = 明文已经到了。父组件拿到响应后把 `plaintext` 放进来，这里就切过去。 */
const showing = computed(() => props.plaintext !== null)

/** 「知道了」勾选；只有重新生成需要——签发不影响任何旧 Key，逼人勾一下只是噪音。 */
const acknowledging = ref(false)

/** 复制后的即时反馈；2 秒后回到「复制」，避免按钮永久停在「已复制」。 */
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | null = null

const confirmText = computed(() =>
  props.mode === 'regenerate' ? '重新生成新 Key' : '签发新 Key',
)

/** 重新生成必须先勾选；签发没有需要确认的代价，永远可点。 */
const confirmDisabled = computed(
  () => props.loading === true || (props.mode === 'regenerate' && !acknowledging.value),
)

const leadText = computed(() =>
  props.mode === 'regenerate'
    ? '已重新生成。下面是它唯一一次的展示：'
    : '已签发。下面是它唯一一次的展示：',
)

function resetCopyFeedback(): void {
  if (copyTimer !== null) {
    clearTimeout(copyTimer)
    copyTimer = null
  }
  copied.value = false
}

watch(open, (isOpen) => {
  if (!isOpen) {
    acknowledging.value = false
    resetCopyFeedback()
  }
})
watch(
  () => props.mode,
  () => {
    acknowledging.value = false
  },
)

onBeforeUnmount(resetCopyFeedback)

async function copyPlaintext(): Promise<void> {
  const value = props.plaintext
  if (value === null) return
  try {
    await navigator.clipboard.writeText(value)
    copied.value = true
    resetCopyFeedback()
    copyTimer = setTimeout(() => {
      copied.value = false
      copyTimer = null
    }, 2000)
  } catch {
    // 剪贴板被拒（非 https / 缺权限）时不假装成功：让用户自己从输入框里全选。
    ElMessage.warning('复制失败，请手动全选复制')
  }
}

/** 聚焦即全选：手选一长串 Key 容易漏字符，而这个框是只读的，全选不会破坏任何东西。 */
function selectAll(event: FocusEvent): void {
  const target = event.target
  if (target instanceof HTMLTextAreaElement) target.select()
}

function cancel(): void {
  emit('cancel')
}
</script>

<template>
  <!--
    展示态锁死三条关闭路径（遮罩 / ESC / 右上角 X）：这些都是一次误触，
    而误触的代价是明文再也取不回来。警示态保持默认可关——那时还没有不可挽回的东西。
  -->
  <el-dialog
    v-model="open"
    append-to-body
    title="获取 Key"
    width="560px"
    :close-on-click-modal="!showing && props.loading !== true"
    :close-on-press-escape="!showing && props.loading !== true"
    :show-close="!showing && props.loading !== true"
  >
    <!-- 警示态 -->
    <div v-if="!showing" data-state="warning" class="space-y-3 text-sm leading-relaxed text-slate-700">
      <p class="m-0">
        这个 Key 只会显示这一次。关闭之后，平台不再保存它的明文，也无法再为你调取——请现在复制并妥善保管。
      </p>
      <template v-if="props.mode === 'regenerate'">
        <p class="m-0">
          重新生成后，当前 Key 会变成过渡 Key：它仍然可用 24 小时，期间新旧两把都能通过校验，方便你把业务换过来。想让它提前失效，随时可以在列表里作废。
        </p>
        <p class="m-0">
          过渡期只保留最近一把：24 小时内再点一次「获取 Key」，上一把过渡 Key 会立刻作废。
        </p>
        <el-checkbox v-model="acknowledging">我知道当前 Key 会在 24 小时后失效</el-checkbox>
      </template>
    </div>

    <!-- 展示态 -->
    <div v-else data-state="reveal" class="space-y-2 text-sm text-slate-700">
      <p class="m-0">{{ leadText }}</p>
      <el-input
        :model-value="props.plaintext ?? ''"
        readonly
        type="textarea"
        :rows="3"
        class="font-mono"
        @focus="selectAll"
      />
      <div class="flex flex-wrap items-center justify-between gap-2">
        <p class="m-0 text-xs text-muted">关闭后无法再次查看。</p>
        <el-button size="small" type="primary" plain @click="copyPlaintext">
          {{ copied ? '已复制' : '复制' }}
        </el-button>
      </div>
    </div>

    <template #footer>
      <div class="flex flex-wrap justify-end gap-2">
        <el-button @click="cancel">关闭</el-button>
        <!--
          展示态的「重新生成」必须消失：这时再点一次会轮换掉正在展示的这一串，
          用户手一滑就把还没复制的 Key 作废了。警示态才是它唯一该出现的地方。
        -->
        <el-button
          v-if="!showing"
          type="primary"
          :disabled="confirmDisabled"
          :loading="props.loading === true"
          @click="emit('confirm')"
        >
          {{ confirmText }}
        </el-button>
        <el-button v-else type="primary" @click="emit('debug')">去调试台试用</el-button>
      </div>
    </template>
  </el-dialog>
</template>
