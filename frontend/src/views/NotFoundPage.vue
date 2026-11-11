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
  <section class="card card-body py-16 text-center sm:py-20">
    <p class="text-[56px] font-semibold leading-none tracking-tightest text-line-strong">404</p>
    <h2 class="page-title mt-4">这个地址没有对应页面</h2>
    <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted">
      路径 <code class="mono-tag">{{ route.fullPath }}</code> 不在调试台的路由里。
      可能是地址写错了，也可能这一页在重构里被并进了别处。
    </p>

    <div class="mt-7 flex flex-wrap justify-center gap-2">
      <RouterLink to="/">
        <el-button type="primary">回到首页</el-button>
      </RouterLink>
      <RouterLink to="/knowledge-bases">
        <el-button>知识库管理</el-button>
      </RouterLink>
    </div>
  </section>
</template>
