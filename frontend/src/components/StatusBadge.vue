<script setup lang="ts">
/**
 * 四态徽章：`status` → 文案 / 颜色的**唯一**入口。
 *
 * 页面只传 `status` 与 `errorMessage`，禁止在页面里写 `status === 1 ? 颜色`：
 * 颜色表一旦分散，上传页叫「索引中」、列表页叫「处理中」，用户会以为后端有两种状态。
 * 状态 3（索引中）与 4（排队中）都不是失败——文案与底色都不能按失败走；
 * 而它们之间也必须看得出区别：一个在等（可以撤回），一个在跑（不能动）。
 */
import { computed } from 'vue'

import { DOCUMENT_STATUS } from '../utils/documentStatus'

interface StatusStyle {
  text: string
  background: string
  color: string
}

const props = defineProps<{
  /** 1 成功 / 2 失败 / 3 索引中 / 4 排队中；其它值不猜，统一显示「未知」。 */
  status: number
  /** 失败时的后端 `error_message`；只有它能被展开查看。 */
  errorMessage?: string | null
}>()

// 颜色来自 `00-facts.md` §10 的语义配色，但**取 tokens.css 里的变量**：
// 这套界面有深色主题，写死浅底徽章会在深色页上变成一块贴纸。
// 每组对比度均 ≥4.5:1（浅色 / 深色各一套值，见 tokens.css 两组变量）。
const STATUS_STYLES: Record<number, StatusStyle> = {
  [DOCUMENT_STATUS.SUCCESS]: {
    text: '成功',
    background: 'rgb(var(--c-ok-bg))',
    color: 'rgb(var(--c-ok))',
  },
  [DOCUMENT_STATUS.FAILED]: {
    text: '失败',
    background: 'rgb(var(--c-fail-bg))',
    color: 'rgb(var(--c-fail))',
  },
  [DOCUMENT_STATUS.PROCESSING]: {
    text: '索引中',
    background: 'rgb(var(--c-indexing-bg))',
    color: 'rgb(var(--c-indexing))',
  },
  // 排队中：比「索引中」更冷、更淡的一档。它不是警告（没有出错），也不该和「成功」的
  // 绿色抢注意力——用户要读到的是「还没轮到」。
  [DOCUMENT_STATUS.QUEUED]: {
    text: '排队中',
    background: 'rgb(var(--c-queued-bg))',
    color: 'rgb(var(--c-queued))',
  },
}
const UNKNOWN_STYLE: StatusStyle = {
  text: '未知',
  background: 'rgb(var(--c-unknown-bg))',
  color: 'rgb(var(--c-unknown))',
}

const badgeStyle = computed(() => STATUS_STYLES[props.status] ?? UNKNOWN_STYLE)

/**
 * 只有失败且确有原文才可展开：成功行也可能残留上一轮的 error_message，
 * 给它一个「详情」入口只会让人以为成功里藏着问题。
 */
const detail = computed(() => {
  if (props.status !== DOCUMENT_STATUS.FAILED) return null
  const message = props.errorMessage?.trim()
  return message ? message : null
})
</script>

<template>
  <el-popover v-if="detail !== null" trigger="click" placement="top" :width="320">
    <template #reference>
      <span
        class="inline-flex cursor-pointer items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium"
        :style="{ backgroundColor: badgeStyle.background, color: badgeStyle.color }"
      >
        {{ badgeStyle.text }}
        <span class="text-xs font-normal opacity-70">详情</span>
      </span>
    </template>
    <p class="m-0 whitespace-pre-wrap break-words text-xs leading-relaxed text-red-700">
      {{ detail }}
    </p>
  </el-popover>

  <span
    v-else
    class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
    :style="{ backgroundColor: badgeStyle.background, color: badgeStyle.color }"
  >
    {{ badgeStyle.text }}
  </span>
</template>
