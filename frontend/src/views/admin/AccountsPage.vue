<!--
  占位文件：由 #95（T13「管理三页」）换成真实的账号管理页（列表 + 停用/换套餐）。
  本票只保证 /admin/accounts 这条路由真实存在——管理员登录后要落地到这里，
  守卫也要能命中它；真实内容不在 T6 范围内，写了就是越界交付。
-->
<template>
  <section class="rounded-lg border border-line bg-white p-6">
    <h2 class="text-base font-semibold text-slate-800">账号管理</h2>
    <p class="mt-2 text-sm text-slate-500">
      本页由 <code class="rounded bg-slate-100 px-1.5 py-0.5">#95 / T13</code> 实现。
      当前只有路由占位：管理员登录后能落到这里、顶栏只显示管理员入口，这就是 T6 的全部验收。
    </p>
  </section>
</template>
