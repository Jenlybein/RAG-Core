<script setup lang="ts">
/**
 * 三态徽章：`status` → 文案 / 颜色的**唯一**入口。
 *
 * 页面只传 `status` 与 `errorMessage`，禁止在页面里写 `status === 1 ? 颜色`：
 * 颜色表一旦分散，上传页叫「索引中」、列表页叫「处理中」，用户会以为后端有两种状态。
 * 状态 3 的语义是「已提交、后台索引」，不是失败——文案与底色都不能按失败走。
 */
import { computed } from 'vue'

interface StatusStyle {
  text: string
  background: string
  color: string
}

const props = defineProps<{
  /** 1 成功 / 2 失败 / 3 索引中；其它值不猜，统一显示「未知」。 */
  status: number
  /** 失败时的后端 `error_message`；只有它能被展开查看。 */
  errorMessage?: string | null
}>()

// 颜色来自 `00-facts.md` §10 / 前端约定「状态与颜色」，不引入第二套令牌。
const STATUS_STYLES: Record<number, StatusStyle> = {
  1: { text: '成功', background: '#D1FAE5', color: '#065F46' },
  2: { text: '失败', background: '#FEE2E2', color: '#B91C1C' },
  3: { text: '索引中', background: '#FEF3C7', color: '#92400E' },
}
const UNKNOWN_STYLE: StatusStyle = { text: '未知', background: '#F3F4F6', color: '#6B7280' }

const badgeStyle = computed(() => STATUS_STYLES[props.status] ?? UNKNOWN_STYLE)

/**
 * 只有失败且确有原文才可展开：成功行也可能残留上一轮的 error_message，
 * 给它一个「详情」入口只会让人以为成功里藏着问题。
 */
const detail = computed(() => {
  if (props.status !== 2) return null
  const message = props.errorMessage?.trim()
  return message ? message : null
})
</script>

<template>
  <el-popover v-if="detail !== null" trigger="click" placement="top" :width="320">
    <template #reference>
      <span
        class="inline-flex cursor-pointer items-center gap-1 rounded px-2 py-0.5 text-xs font-medium"
        :style="{ backgroundColor: badgeStyle.background, color: badgeStyle.color }"
      >
        {{ badgeStyle.text }}
        <span class="text-[10px] font-normal opacity-70">详情</span>
      </span>
    </template>
    <p class="m-0 whitespace-pre-wrap break-words text-xs leading-relaxed text-red-700">
      {{ detail }}
    </p>
  </el-popover>

  <span
    v-else
    class="inline-flex items-center rounded px-2 py-0.5 text-xs font-medium"
    :style="{ backgroundColor: badgeStyle.background, color: badgeStyle.color }"
  >
    {{ badgeStyle.text }}
  </span>
</template>
