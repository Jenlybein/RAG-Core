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
  <div class="min-h-screen bg-canvas">
    <AppHeader v-if="withHeader" />

    <main class="mx-auto max-w-5xl px-6 py-6">
      <RouterView />
    </main>
  </div>
</template>
