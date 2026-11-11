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
  <!--
    吸顶磨砂顶栏（apple.com 的招牌）。三条取舍：

    - **半透明白 + backdrop-blur**：苹果的导航就是内容从底下微微透上来。上一版这里是
      不透明的，理由是「滚动时半透明色块像在动」；这次按设计语言的要求改回磨砂，并把
      不透明度提到 80%、叠一层 saturate，透出来的内容足够模糊，不会被读成「栏在动」。
    - **保留一条 1px 底边**：模糊之外还需要一条硬边界，否则浅色页面上栏与内容会糊在一起。
    - 不再加投影：苹果的导航没有投影，靠模糊 + 细线定形。
  -->
  <header
    class="sticky top-0 z-30 border-b border-line/70 bg-white/80 backdrop-blur-xl backdrop-saturate-150"
  >
    <div
      class="mx-auto flex w-full max-w-page flex-wrap items-center justify-between gap-3 px-5 py-3 sm:px-8"
    >
      <div class="flex flex-wrap items-center gap-5">
        <!--
          品牌标记：一个近黑圆角方块里放一个极简的字形。不引图标库——这类位置一个字母
          就够，vector 图标反而会抢走标题的注意力。
        -->
        <RouterLink to="/" class="flex items-center gap-2.5">
          <span
            class="grid h-7 w-7 place-items-center rounded-[9px] bg-accent text-[13px] font-semibold leading-none text-white shadow-[0_1px_2px_rgba(28,26,21,0.22)]"
            aria-hidden="true"
            >R</span
          >
          <span class="text-[13px] font-semibold tracking-tight text-ink">RAG-Core</span>
          <span class="hidden text-[13px] text-muted sm:inline">调试台</span>
        </RouterLink>

        <!-- 未登录或角色未知时 items 为空，导航整块不渲染：不做「猜一个角色」的兜底入口。 -->
        <nav v-if="items.length > 0" class="flex flex-wrap items-center gap-0.5">
          <RouterLink
            v-for="item in items"
            :key="item.to"
            :to="item.to"
            class="nav-pill"
            :class="
              isCurrent(item.to)
                ? 'bg-slate-100 font-medium text-ink'
                : 'text-slate-600 hover:bg-slate-100 hover:text-ink'
            "
          >
            {{ item.label }}
          </RouterLink>
        </nav>
      </div>

      <div class="flex items-center gap-3 text-sm">
        <!-- 身份做成一个胶囊：名字是主色，角色是灰字——比「bob（租户账号）」整串更易扫读 -->
        <span
          v-if="account !== null"
          class="flex items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1 text-[12px] shadow-[0_1px_1px_rgba(28,26,21,0.03)]"
        >
          <span class="font-medium text-ink">{{ account.username }}</span>
          <span class="text-muted">·</span>
          <span class="text-muted">{{ roleLabel }}</span>
        </span>
        <el-button size="small" :loading="loggingOut" @click="logout">登出</el-button>
      </div>
    </div>
  </header>
</template>
