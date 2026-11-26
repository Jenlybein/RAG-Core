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
import ThemeSwitch from './ThemeSwitch.vue'

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
    顶栏 = 一条透明的吸顶栏 + 一块浮在它中间的玻璃胶囊。

    为什么不整条做成玻璃带：设计稿里顶栏是一枚胶囊，左右留出画布；整条铺满会把
    「内容是一张纸」的层次压平。但胶囊要自己把滚过的内容盖住 —— 栏高 = 胶囊高
    （底部不留内距），填充足够实（nav 那档），否则内容会从胶囊边缘露出来。

    这一条契约由 `appHeaderSticky.spec.ts` 守着：sticky 吸顶、磨砂有实底、有一条硬边。
    断言从 header 元素迁到了下面的胶囊（`data-header-capsule`）——视觉上承担这三件事的
    已经是胶囊，不是那条透明栏。
  -->
  <header class="sticky top-0 z-30">
    <div class="mx-auto w-full max-w-page px-4 pt-3 sm:px-6">
      <div
        data-header-capsule
        class="flex flex-wrap items-center gap-3 rounded-[22px] border border-white/50 bg-white/85 px-3.5 py-2.5 shadow-nav backdrop-blur-xl backdrop-saturate-150"
      >
        <!--
          窄屏两行、桌面一行：
          · 窄屏：第一行「品牌 —— 登出」，第二行整条导航横滑。五项入口挤在一行会把胶囊
            撑出屏幕，右侧的账号与登出直接看不见（这是实测出来的，不是假设）。
          · 桌面：品牌 + 导航 + 账号三块并排，导航 flex-1 吃掉多余宽度。
        -->
        <div class="flex w-full items-center justify-between gap-3 md:w-auto md:shrink-0">
          <RouterLink to="/" class="flex shrink-0 items-center gap-2.5">
            <span
              class="grid h-7 w-7 place-items-center rounded-[9px] bg-accent text-[13px] font-semibold leading-none text-white shadow-[0_6px_16px_-6px_rgba(0,90,190,0.8)]"
              aria-hidden="true"
              >R</span
            >
            <span class="text-[13px] font-semibold tracking-tight text-ink">RAG-Core</span>
          </RouterLink>

          <!-- 窄屏的登出放这一行右侧：第二行整条给了导航，没有它的位置 -->
          <el-button size="small" class="md:hidden" :loading="loggingOut" @click="logout">
            登出
          </el-button>
        </div>

        <!-- 未登录或角色未知时 items 为空，导航整块不渲染：不做「猜一个角色」的兜底入口。 -->
        <nav
          v-if="items.length > 0"
          class="nav-track order-3 -mx-1 flex w-full min-w-0 items-center gap-0.5 overflow-x-auto px-1 md:order-none md:w-auto md:flex-1"
        >
          <RouterLink
            v-for="item in items"
            :key="item.to"
            :to="item.to"
            class="nav-pill shrink-0 whitespace-nowrap"
            :class="isCurrent(item.to) ? 'is-active text-ink' : 'text-muted'"
          >
            {{ item.label }}
          </RouterLink>
        </nav>

        <div class="hidden shrink-0 items-center gap-2.5 md:flex">
          <ThemeSwitch />
          <!-- 身份只留名字：角色已经由导航条目说明，重复一遍是噪声 -->
          <span v-if="account !== null" class="flex items-center gap-1.5 text-[12px] text-ink">
            <span class="font-medium">{{ account.username }}</span>
          </span>
          <el-button size="small" :loading="loggingOut" @click="logout">登出</el-button>
        </div>
      </div>
    </div>
  </header>
</template>
