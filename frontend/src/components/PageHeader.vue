<script setup lang="ts">
/**
 * 页面头：大字标题 + 一行说明，右侧可选放操作。
 *
 * 单独做成组件而不是每页写一遍 `<h1 class="...">`：标题的字号/字距/间距是这套观感
 * 最吃紧的一处，散在 10 个页面里必然有一页写成 20px、另一页写成 18px。
 */
defineProps<{
  title: string
  /** 一两句话说明这一页在干什么；不传则不留空白行。 */
  subtitle?: string
}>()
</script>

<template>
  <div class="flex flex-wrap items-end justify-between gap-4 pb-1">
    <div>
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="subtitle" class="page-subtitle">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.actions" class="flex flex-wrap items-center gap-2">
      <slot name="actions" />
    </div>
  </div>
</template>
