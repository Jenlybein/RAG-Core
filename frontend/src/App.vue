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
      顶部一层极淡的暖光：只是把「页面是一张纸」这件事说清楚，不给任何内容加对比度。
      用 `absolute` 而不是 `fixed`——fixed 会跟着视口走，滚动时页面顶部一直被洗白，
      看着像有东西在动；absolute 让它随内容一起滚走，只在页面顶端出现一次。
    -->
    <!--
      顶部一层极淡的白色：苹果的页面是**纯平**的，没有渐变；这里只留一层几乎看不见的
      提亮，让内容列与画布之间有一点呼吸。去掉原来那层暖色（#FDFCF8）——换到冷灰画布后
      它会显出一块黄。
    -->
    <div
      class="pointer-events-none absolute inset-x-0 top-0 h-72 bg-gradient-to-b from-white to-transparent opacity-70"
      aria-hidden="true"
    />

    <AppHeader v-if="withHeader" />

    <main class="relative mx-auto w-full max-w-page px-5 py-10 sm:px-8 sm:py-14">
      <RouterView />
    </main>
  </div>
</template>
