<!--
  临时占位页，T6(#85) 引入、由后续票替换：
  /（#84 上传）、/knowledge-bases（#86 列表）、/keys（#92 Key 页）、/retrieve（#87 检索）
  的真实页面落地后，各自的路由条目改成真组件，本文件直接删掉。
  为什么先放占位而不预埋空组件：守卫与顶栏都要能命中真实路由，否则登录成功后
  跳过去是一片空白，验收里「成功进入知识库列表」就无法成立。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => route.meta.placeholderTitle ?? '未命名页面')
const issue = computed(() => route.meta.placeholderIssue ?? '后续票')
</script>

<template>
  <section class="rounded-lg border border-line bg-white p-6">
    <h2 class="text-base font-semibold text-slate-800">{{ title }}</h2>
    <p class="mt-2 text-sm text-slate-500">
      这一页由 <code class="rounded bg-slate-100 px-1.5 py-0.5">{{ issue }}</code> 实现。
      T6(#85) 只放一条能被路由守卫与顶栏命中的占位路由，不含真实内容。
    </p>
  </section>
</template>
