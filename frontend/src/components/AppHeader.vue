<script setup lang="ts">
/**
 * 顶栏：唯一按角色分流入口的地方。
 *
 * 刻意不接收 props：它是布局层的一部分，入口来自「当前账号」而不是某个页面；
 * 若由页面传进来，就会出现两个页面各传一份、彼此不一致的可能。
 *
 * 角色来源是 `GET /console/me`（与路由守卫同一个接口）：cookie 是 HttpOnly，
 * 前端不做也不该做「记住角色」的本地缓存——刷新、换人登录时都会自然重查。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '../lib/envelope'
import { CONSOLE_ME_QUERY_KEY, fetchMe, submitLogout } from '../services/console'
import { navFor } from '../utils/access'

const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()

// staleTime 0 + retry 关掉：身份宁可多读一次，也不要拿旧的一秒内缓存去渲染别人的入口。
const meQuery = useQuery({
  queryKey: CONSOLE_ME_QUERY_KEY,
  queryFn: fetchMe,
  retry: false,
})

const account = computed(() => meQuery.data.value?.account ?? null)
const items = computed(() => navFor(account.value?.role))
const roleLabel = computed(() => (account.value?.role === 'admin' ? '管理员' : '租户账号'))

const loggingOut = ref(false)

/**
 * 当前链接是否高亮。不能直接用 RouterLink 的 active-class：`/` 是每条路径的前缀，
 * 那样「上传文档」在知识库页也会亮着。
 */
function isCurrent(to: string): boolean {
  return to === '/' ? route.path === '/' : route.path.startsWith(to)
}

async function logout(): Promise<void> {
  loggingOut.value = true
  try {
    await submitLogout()
    // 清掉身份缓存：服务端会话已删，浏览器里不能还留着一份「上一个账号」的读数。
    queryClient.removeQueries({ queryKey: CONSOLE_ME_QUERY_KEY })
    await router.push('/login')
  } catch (error) {
    // 登出失败不改页面状态：会话可能还在，贸然跳登录页会让用户以为已经登出了。
    ElMessage.error(error instanceof ApiError ? error.message : String(error))
  } finally {
    loggingOut.value = false
  }
}
</script>

<template>
  <header class="border-b border-line bg-white">
    <div class="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3 px-6 py-3">
      <div class="flex flex-wrap items-center gap-6">
        <span class="text-sm font-semibold text-brand">RAG-Core 调试台</span>
        <!-- 未登录或角色未知时 items 为空，导航整块不渲染：不做「猜一个角色」的兜底入口。 -->
        <nav v-if="items.length > 0" class="flex flex-wrap items-center gap-4 text-sm">
          <RouterLink
            v-for="item in items"
            :key="item.to"
            :to="item.to"
            class="hover:text-brand"
            :class="isCurrent(item.to) ? 'font-medium text-brand' : 'text-slate-600'"
          >
            {{ item.label }}
          </RouterLink>
        </nav>
      </div>
      <div class="flex items-center gap-3 text-sm">
        <span v-if="account !== null" class="text-slate-500">
          {{ account.username }}（{{ roleLabel }}）
        </span>
        <el-button size="small" :loading="loggingOut" @click="logout">登出</el-button>
      </div>
    </div>
  </header>
</template>
