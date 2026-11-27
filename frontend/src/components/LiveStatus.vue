<script setup lang="ts">
import { computed } from 'vue'

import { refreshModeLabel, type RefreshMode } from '../lib/refreshPolicy'

/**
 * 一行「这份数据有多旧」：`更新于 14:03:12 · 自动刷新中（每 3 秒）` + 手动刷新。
 *
 * ## 为什么这个组件存在
 *
 * 一个会停的界面必须自证它停在哪一刻。以前轮询在「全终态」时静默 `false`，界面上没有任何
 * 变化——于是「已经刷新完，数据是新的」与「刷新停了，数据是十分钟前的」长得一模一样，
 * 唯一出路是 F5。这个组件把那句话补上：**时间戳 + 当前档位 + 一个能点的刷新**。
 *
 * 时间取的是**服务端**给的 `generated_at`（不是浏览器收到响应的时刻）：跨时区、慢网络、
 * 浏览器时钟漂移都不会让它说谎。
 */

const props = defineProps<{
  /** 服务端打的 ISO 时间戳；`null` 表示还没有任何一次成功读取。 */
  updatedAt: string | null
  mode: RefreshMode
  /** 正在重取（按钮转圈用）。 */
  refreshing?: boolean
}>()

const emit = defineEmits<{ refresh: [] }>()

/**
 * 只显示时分秒：这一行的读者在判断「新不新」，不在读日历。
 *
 * `toLocaleTimeString` 走本地时区——`generated_at` 是 UTC 的 ISO 串，
 * 直接截字符串会让 05:33Z 在 +08 的屏幕上显示成 05:33。
 */
const clock = computed<string>(() => {
  if (props.updatedAt === null) return '尚未读取'
  const at = new Date(props.updatedAt)
  if (Number.isNaN(at.getTime())) return '时间未知'
  return at.toLocaleTimeString('zh-CN', { hour12: false })
})

const label = computed<string>(() => refreshModeLabel(props.mode))
</script>

<template>
  <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
    <span class="tabular-nums">更新于 {{ clock }}</span>
    <span class="text-slate-300">·</span>
    <span :class="mode === 'live' ? 'text-sky-700' : ''">{{ label }}</span>
    <el-button
      link
      type="primary"
      size="small"
      :loading="refreshing === true"
      @click="emit('refresh')"
    >
      刷新
    </el-button>
  </div>
</template>
