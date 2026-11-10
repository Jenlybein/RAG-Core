<script setup lang="ts">
/**
 * 404 页：只做一件事——把「地址写错了」与「页面挂了」区分开。
 *
 * 没有兜底路由时，输错地址得到的是**空白 main**：顶栏还在、导航还亮着，看起来像界面
 * 坏了。这一页存在的全部意义是给人一句确定的话，以及一个回去的入口。
 *
 * 不猜测「你可能是想找 X」：最近的路径猜测在路由改名之后只会更错。
 */
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <section class="card card-body">
    <h2 class="page-title">这个地址没有对应页面</h2>
    <p class="page-subtitle">
      路径 <span class="font-mono text-ink">{{ route.fullPath }}</span> 不在调试台的任何一条路由里。
      可能是地址写错了，也可能这一页在重构里被并进了别的页面。
    </p>

    <div class="mt-6 flex flex-wrap gap-2">
      <RouterLink to="/">
        <el-button type="primary">回到首页</el-button>
      </RouterLink>
      <RouterLink to="/knowledge-bases">
        <el-button>知识库管理</el-button>
      </RouterLink>
    </div>
  </section>
</template>
