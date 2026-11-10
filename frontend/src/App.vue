<script setup lang="ts">
// 顶栏只挂一次；本票不按角色分流入口（那是 T6 的事），只留一个去探针页的链接。
</script>

<template>
  <div class="min-h-screen bg-canvas">
    <header class="border-b border-line bg-white">
      <div class="mx-auto flex max-w-5xl items-center justify-between px-6 py-3">
        <div class="flex items-center gap-2">
          <span class="text-sm font-semibold text-brand">RAG-Core 调试台</span>
          <span class="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-500">T1 骨架</span>
        </div>
        <nav class="text-sm">
          <RouterLink class="text-brand hover:underline" to="/probe">探针页</RouterLink>
        </nav>
      </div>
    </header>

    <main class="mx-auto max-w-5xl px-6 py-6">
      <RouterView />
    </main>
  </div>
</template>
