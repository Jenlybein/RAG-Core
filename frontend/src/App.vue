<script setup lang="ts">
/**
 * 布局层：顶栏只在这里挂一次。
 *
 * 布局用路由 `meta.layout` 判断，不按 `path === '/login'` 比较：布局是路由的属性，
 * 按路径判断会把「某个字符串」和「一种布局」绑死，将来再加一个无顶栏页面
 * （独立错误页、打印页）就得回来改一次条件。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from './components/AppHeader.vue'

const route = useRoute()
const withHeader = computed(() => route.meta.layout !== 'blank')
</script>

<template>
  <div class="relative min-h-screen bg-canvas">
    <!--
      顶部一层极淡的暖光：它不给任何内容加对比度，只是把「页面是一张纸」这件事说清楚。
      用 fixed + pointer-events-none，滚动时不跟着动、也不吃点击。
    -->
    <div
      class="pointer-events-none fixed inset-x-0 top-0 h-64 bg-gradient-to-b from-[#FDFCF8] to-transparent"
      aria-hidden="true"
    />

    <AppHeader v-if="withHeader" />

    <main class="relative mx-auto w-full max-w-page px-5 py-8 sm:px-8 sm:py-10">
      <RouterView />
    </main>
  </div>
</template>
