<script setup lang="ts">
/**
 * 受控二次确认弹窗。
 *
 * 只抛 `confirm` 事件、自己不调接口：删除与重建的「请求」和「确认」分开，
 * 页面才能在同一个弹窗背后挂两件不可撤销的事，而弹窗不必知道是哪一件。
 * `open` 用 `defineModel` 由调用方持有：关闭权属于发起动作的页面，
 * 弹窗内部不能自行关闭，否则成功后想再展示一次结果就没有机会。
 */
const open = defineModel<boolean>({ required: true })

defineProps<{
  title: string
  message: string
  /** 确认按钮文案；默认「确认」，删除类应写「删除」这类动词而不要写「确定」。 */
  confirmText?: string
  /** 删除等不可撤销动作 → 红色确认按钮；重建等中性动作保持主色。 */
  danger?: boolean
  /** 请求在途时锁住弹窗：避免重复点确认或点了取消但请求已经发出。 */
  loading?: boolean
  /**
   * 确认按钮不可用（动作在当前状态下不被后端接受）。
   *
   * 与 `loading` 分开：`loading` 是「正在做」，这里是「做不了」。例如删库只允许空库，
   * 库内还有文档时后端会回 20015——按钮留着但点不动，比藏起来更能说清规则：
   * 用户看到的是「删不了」而不是「没有删除这回事」。
   */
  confirmDisabled?: boolean
}>()

const emit = defineEmits<{ confirm: [] }>()
</script>

<template>
  <el-dialog
    v-model="open"
    append-to-body
    :title="title"
    width="480px"
    :close-on-click-modal="loading !== true"
    :close-on-press-escape="loading !== true"
    :show-close="loading !== true"
  >
    <p class="m-0 whitespace-pre-wrap break-words text-sm leading-relaxed text-slate-700">
      {{ message }}
    </p>

    <template #footer>
      <div class="flex justify-end gap-2">
        <el-button :disabled="loading === true" @click="open = false">取消</el-button>
        <el-button
          :type="danger === true ? 'danger' : 'primary'"
          :disabled="confirmDisabled === true"
          :loading="loading === true"
          @click="emit('confirm')"
        >
          {{ confirmText ?? '确认' }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>
