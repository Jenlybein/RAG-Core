<script setup lang="ts">
/**
 * 登录页：账号 + 口令换服务端会话（HttpOnly cookie，Path=/console）。
 *
 * 前端不存任何登录凭证、也不把角色记进 localStorage：刷新后的登录态只来自 cookie，
 * 每次进页面都由 `GET /console/me` 现问一次（这就是验收里「刷新后仍在」的实现方式）。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '../lib/envelope'
import { fetchMe, submitLogin } from '../services/console'

const route = useRoute()
const router = useRouter()

const username = ref('')
const password = ref('')
const busy = ref(false)
const errorText = ref('')

/** 守卫在「拿不到业务码」时会把离线标记放进 query：掉线不是账号问题，必须分开提示。 */
const backendOffline = computed(() => route.query.offline === '1')

/**
 * 登录失败是「同码不同因」的典型：20010 在这里是账号/口令不对，在守卫那里才是会话过期；
 * 20005 是秒级限流，等一会儿就能成功。把码翻成这两句，避免用户去查根本不存在的登录态问题。
 */
function failureText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 20010) return '账号或口令不对'
    if (error.code === 20005) return '尝试过于频繁，稍后再试'
    // 其它码（例如 20002）保留后端/统一文案原文，别让人只看到「登录失败」四个字。
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}

async function submit(): Promise<void> {
  errorText.value = ''
  if (username.value.trim() === '' || password.value === '') {
    errorText.value = '账号和口令都要填'
    return
  }

  busy.value = true
  try {
    await submitLogin(username.value, password.value)
  } catch (error) {
    errorText.value = failureText(error)
    busy.value = false
    return
  }

  try {
    // 登录响应里其实也带了 MeData，但这里重查一次：确认 cookie 真的在浏览器里生效，
    // 并让「按角色跳转」与守卫用同一个接口、同一份结论。
    const me = await fetchMe()
    await router.replace(me.account.role === 'admin' ? '/admin/accounts' : '/knowledge-bases')
  } catch (error) {
    errorText.value = `登录已通过，但读取当前账号失败：${failureText(error)}`
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!--
    登录页是一张「封面」：不套顶栏、垂直居中、四周留足空白。
    它是整站第一印象，也是唯一一页可以不讲信息密度的地方——所以这里给品牌标记、
    大标题与一句克制的说明，而不是把表单塞满。
  -->
  <div class="mx-auto flex min-h-[70vh] w-full max-w-[380px] flex-col justify-center">
    <div
      v-if="backendOffline"
      class="mb-4 rounded-control border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
    >
      <p class="font-semibold">后端连不上</p>
      <p class="mt-1 leading-relaxed">
        登录请求没有拿到业务码（不是账号问题）。确认后端已启动后再试。
      </p>
    </div>

    <section class="card px-7 py-9 shadow-lift">
      <span
        class="grid h-10 w-10 place-items-center rounded-[13px] bg-accent text-base font-semibold text-white shadow-[0_2px_6px_rgba(28,26,21,0.22)]"
        aria-hidden="true"
        >R</span
      >
      <h1 class="mt-5 text-[26px] font-semibold leading-tight tracking-tightest text-ink">
        登录调试台
      </h1>
      <p class="mt-1.5 text-[13px] leading-relaxed text-muted">
        用控制台账号登录。登录态由服务端会话维护，刷新不会掉。
      </p>

      <form class="mt-7 space-y-4" @submit.prevent="submit">
        <label class="block">
          <span class="mb-1.5 block text-[12px] font-medium text-slate-600">账号</span>
          <el-input
            v-model="username"
            size="large"
            placeholder="username"
            autocomplete="username"
            :disabled="busy"
          />
        </label>
        <label class="block">
          <span class="mb-1.5 block text-[12px] font-medium text-slate-600">口令</span>
          <el-input
            v-model="password"
            size="large"
            type="password"
            show-password
            placeholder="password"
            autocomplete="current-password"
            :disabled="busy"
          />
        </label>

        <p
          v-if="errorText !== ''"
          class="rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm leading-relaxed text-red-800"
        >
          {{ errorText }}
        </p>

        <el-button
          type="primary"
          native-type="submit"
          size="large"
          class="!mt-6 w-full"
          :loading="busy"
        >
          登录
        </el-button>
      </form>
    </section>
  </div>
</template>
