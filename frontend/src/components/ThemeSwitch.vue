<script setup lang="ts">
/**
 * 主题三态开关：跟随 / 浅 / 深。
 *
 * 为什么是三个而不是一个「深色开关」：单开关表达不了「跟随系统」——用户在深色系统下
 * 想切浅色时，开关的「关」仍然是跟随（深色），点了没反应，看起来像坏了。
 *
 * 用 button 而不是 radio：这里已经有 JS，`aria-pressed` 比「视觉隐藏的 radio + label」
 * 更直接，键盘行为也由浏览器原生给。
 */
import { computed, ref } from 'vue'

import { readMode, setMode, type ThemeMode } from '../lib/theme'

const mode = ref<ThemeMode>(readMode())

interface Option {
  value: ThemeMode
  label: string
}

const options: readonly Option[] = [
  { value: 'auto', label: '跟随系统' },
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
]

function pick(next: ThemeMode): void {
  mode.value = next
  setMode(next)
}

const current = computed(() => options.find((option) => option.value === mode.value) ?? options[0])
</script>

<template>
  <div class="theme-switch" role="group" :aria-label="`主题：${current.label}`">
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      class="theme-opt"
      :class="{ 'is-on': mode === option.value }"
      :aria-pressed="mode === option.value"
      :title="option.label"
      @click="pick(option.value)"
    >
      <!-- 跟随：一个被对半填色的圆；浅：太阳；深：月亮。都用 1.8px 描边，同粗细。 -->
      <svg
        v-if="option.value === 'auto'"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="8" />
        <path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none" />
      </svg>
      <svg
        v-else-if="option.value === 'light'"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-linecap="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="4.2" />
        <path
          d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"
        />
      </svg>
      <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <path d="M20 14.2A8.4 8.4 0 1 1 9.8 3.8 6.8 6.8 0 0 0 20 14.2z" />
      </svg>
    </button>
  </div>
</template>
