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
  <!--
    画布是**一整块平的中性灰**：不放渐变、不放彩色光晕。
    层次全部由玻璃面自己给（半透明 + 模糊 + 亮边 + 两层投影），
    背景一旦也带层次，两层就打架，看起来像没擦干净的屏幕。
  -->
  <div class="relative flex min-h-dvh flex-col bg-canvas">
    <AppHeader v-if="withHeader" />

    <main class="relative mx-auto w-full max-w-page flex-1 px-5 pb-14 pt-8 sm:px-8 sm:pt-10">
      <RouterView />
    </main>
  </div>
</template>
