<!-- 临时页，T6 后删除：本页只为在浏览器里亲手验完两条身份通道，验完即弃，不并入正式导航。 -->
<script setup lang="ts">
/**
 * 临时探针页，T6 后删除。
 *
 * 存在的唯一理由：把「数据面只用 Bearer、控制面只用 cookie + 防跨站头」这两条通道
 * 各自打通一次，并且把失败也变成看得懂的结论（业务码而不是「请求失败」）。
 * 这里刻意不做登录页、守卫、列表页：那些是 T3/T4/T6 的范围，探针页不该提前长成产品。
 */
import { Refresh } from '@element-plus/icons-vue'
import axios from 'axios'
import { computed, ref } from 'vue'

import { toApiError } from '../lib/axiosError'
import { consoleHttp } from '../lib/consoleHttp'
import { dataHttp } from '../lib/dataHttp'
import { ApiError } from '../lib/envelope'

interface ConsoleMeData {
  account: { username: string; role: string; tenant_id: string | null }
  tenant: { tenant_id: string; name: string } | null
  plan: unknown
  usage: { used: number | null; limit: number | null; source: string } | null
}

interface RetrieveData {
  retrieved_chunks: Array<{ chunk_id: string; title: string; score: number | null }>
  metadata: {
    mode: string
    top_k: number
    retrieval: {
      degraded: boolean
      degraded_reason: string | null
      empty_reason: string | null
    }
  }
}

interface KnowledgeBaseListData {
  knowledge_bases: Array<{ kb_id: string; name: string }>
}

type Tone = 'ok' | 'warn' | 'fail'

interface ProbeLogEntry {
  seq: number
  label: string
  tone: Tone
  text: string
}

const entries = ref<ProbeLogEntry[]>([])
const busy = ref<string | null>(null)
let seq = 0

const kbIdsInput = ref('')
const query = ref('调试台探针：检索链路是否通')
const username = ref('')
const password = ref('')

const baseUrlHint = computed(() => import.meta.env.VITE_API_BASE_URL || '（空，走 Vite 同源代理）')
const apiKeyHint = computed(() => {
  const key = (import.meta.env.API_KEY ?? '').trim()
  if (key === '') return '（未配置）'
  return key.length <= 7 ? key : `${key.slice(0, 3)}***${key.slice(-2)}`
})
const kbIds = computed(() =>
  kbIdsInput.value
    .split(/[，,、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean),
)

const toneClass: Record<Tone, string> = {
  ok: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  warn: 'border-amber-200 bg-amber-50 text-amber-800',
  fail: 'border-red-200 bg-red-50 text-red-800',
}

function push(label: string, tone: Tone, text: string): void {
  entries.value.unshift({ seq: ++seq, label, tone, text })
}

function clearLog(): void {
  entries.value = []
}

function formatError(error: unknown): string {
  if (error instanceof ApiError) {
    // code=-1 是前端自己发现的协议/连通性问题，没有「业务码」可报。
    return error.code === -1 ? error.message : `${error.message}（业务码 ${error.code}）`
  }
  return String(error)
}

async function runAction(label: string, action: () => Promise<void>): Promise<void> {
  busy.value = label
  try {
    await action()
  } catch (error) {
    push(label, 'fail', formatError(error))
  } finally {
    busy.value = null
  }
}

/** 1. 控制面读：没登录时必须拿到 20010，而不是网络错误或白屏。 */
function checkMe(): Promise<void> {
  return runAction('GET /console/me', async () => {
    try {
      const me = await consoleHttp.get<ConsoleMeData>('/console/me')
      const tenantText =
        me.tenant === null ? '管理员（无租户）' : `租户 ${me.tenant.name || me.tenant.tenant_id}`
      push(
        'GET /console/me',
        'ok',
        `已登录：${me.account.username}（${me.account.role}），${tenantText}`,
      )
    } catch (error) {
      if (error instanceof ApiError && error.code === 20010) {
        push('GET /console/me', 'warn', `需要登录（20010）：${error.message}。这是业务码，不是网络错误。`)
        return
      }
      throw error
    }
  })
}

/** 辅助动作：用同一把 dev-key 列库，把 kb_id 填进检索输入框（不是第五件验收物）。 */
function fillKbIds(): Promise<void> {
  return runAction('GET /api/v1/knowledge-bases', async () => {
    const data = await dataHttp.get<KnowledgeBaseListData>('/api/v1/knowledge-bases')
    const kbs = data.knowledge_bases ?? []
    if (kbs.length === 0) {
      push('GET /api/v1/knowledge-bases', 'warn', '这把 Key 的白名单里没有知识库：先去建库或改密钥配置')
      return
    }
    if (kbIds.value.length === 0) {
      kbIdsInput.value = kbs[0]!.kb_id
    }
    push(
      'GET /api/v1/knowledge-bases',
      'ok',
      `白名单 ${kbs.length} 个库：${kbs.map((kb) => `${kb.name}(${kb.kb_id})`).join('、')}`,
    )
  })
}

/**
 * 2. 数据面写（检索）：请求体只有 kb_ids + query，多余字段会被 extra=forbid 拒掉。
 * 空 kb_ids 会得到 20002——那也是「明确的业务码」，不算网络错误。
 */
function retrieve(): Promise<void> {
  return runAction('POST /api/v1/rag/retrieve', async () => {
    const result = await dataHttp.post<RetrieveData>('/api/v1/rag/retrieve', {
      kb_ids: kbIds.value,
      query: query.value,
    })
    const chunks = result.retrieved_chunks ?? []
    const retrieval = result.metadata.retrieval
    if (chunks.length === 0) {
      push(
        'POST /api/v1/rag/retrieve',
        'warn',
        `业务成功但没有切片：empty_reason=${retrieval.empty_reason ?? 'null'}（不是故障）`,
      )
      return
    }
    push(
      'POST /api/v1/rag/retrieve',
      'ok',
      `拿到 ${chunks.length} 条切片；mode=${result.metadata.mode}，top_k=${result.metadata.top_k}，` +
        `degraded=${retrieval.degraded}${retrieval.degraded_reason ? `（${retrieval.degraded_reason}）` : ''}`,
    )
  })
}

/** 3. 控制面写（登录）：成功后 cookie 由后端种下；HttpOnly，所以这里读不到也无需读。 */
function login(): Promise<void> {
  return runAction('POST /console/login', async () => {
    try {
      const me = await consoleHttp.post<ConsoleMeData>('/console/login', {
        username: username.value,
        password: password.value,
      })
      push(
        'POST /console/login',
        'ok',
        `已登录：${me.account.username}（${me.account.role}）。console_session 是 HttpOnly，` +
          '去 DevTools → Application → Cookies 确认，别用 document.cookie 找。',
      )
    } catch (error) {
      // 登录接口把「账号/口令不对」也编成 20010；它和「会话过期」同码不同因，别让用户误以为掉线。
      if (error instanceof ApiError && error.code === 20010) {
        push('POST /console/login', 'warn', '登录未成功（20010）：账号或口令不对。登录接口也用这个码。')
        return
      }
      throw error
    }
  })
}

/**
 * 4. 防跨站头：先裸 axios 发不带头的写请求（预期 20011），再用 consoleHttp 发带头的（预期成功）。
 *
 * 顺序不能反：登出一旦成功就没有会话了，但这里的判据是「头」而不是会话——后端 logout
 * 只查防跨站头，不要求会话有效，所以即便没登录，这两步也应该分别得到 20011 与成功。
 */
function verifyWriteHeader(): Promise<void> {
  return runAction('POST /console/logout（两组）', async () => {
    try {
      await axios.post('/console/logout')
      push(
        '不带 x-requested-with',
        'fail',
        '预期 20011，却成功了：后端没检查防跨站头，或这个请求被别的东西补上了头',
      )
    } catch (error) {
      const apiError = toApiError(error, 'console')
      if (apiError.code === 20011) {
        push('不带 x-requested-with', 'ok', `如预期拿到 20011：${apiError.message}`)
      } else {
        push('不带 x-requested-with', 'fail', `预期 20011，实际 ${apiError.code}：${apiError.message}`)
      }
    }

    await consoleHttp.post<null>('/console/logout')
    push(
      '带 x-requested-with',
      'ok',
      '写请求成功，cookie 已被服务端与浏览器两侧清掉；上面的 20011 没有被当成掉登录。',
    )
  })
}
</script>

<template>
  <section class="space-y-5">
    <div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
      <p class="font-semibold">临时页，T6 后删除</p>
      <p class="mt-1">
        这一页只验证两件事：<strong>数据面靠 Bearer</strong>、<strong>控制面靠 cookie + 防跨站头</strong>。
        它没有登录页、守卫、列表页，也不会进正式导航。
      </p>
    </div>

    <div class="rounded-lg border border-line bg-white px-4 py-3 text-sm text-slate-600">
      <div class="grid gap-2 md:grid-cols-2">
        <p>
          <span class="text-slate-400">VITE_API_BASE_URL：</span><code>{{ baseUrlHint }}</code>
        </p>
        <p>
          <span class="text-slate-400">API_KEY：</span><code>{{ apiKeyHint }}</code>
          <span class="ml-2 text-slate-400">（只来自 .env，后续票才换成页面选中的 Key）</span>
        </p>
      </div>
      <p class="mt-2 text-xs text-slate-400">
        两条通道都经 Vite 的 /api 与 /console 同源代理走；浏览器里不应出现任何跨域报错。
      </p>
    </div>

    <div class="grid gap-4 lg:grid-cols-2">
      <article class="rounded-lg border border-line bg-white p-4 shadow-sm">
        <h2 class="text-sm font-semibold text-slate-800">1. 控制面读：我的视图</h2>
        <p class="mt-1 text-xs text-slate-500">
          未登录时应显示「需要登录（20010）」；如果是「连不上后端」，那是环境没起，不是身份问题。
        </p>
        <div class="mt-3">
          <el-button
            type="primary"
            :disabled="busy !== null"
            :loading="busy === 'GET /console/me'"
            @click="checkMe"
          >
            GET /console/me
          </el-button>
        </div>
      </article>

      <article class="rounded-lg border border-line bg-white p-4 shadow-sm">
        <h2 class="text-sm font-semibold text-slate-800">2. 数据面：用 dev-key 检索</h2>
        <p class="mt-1 text-xs text-slate-500">
          请求体只有 <code>kb_ids</code> + <code>query</code>。先填一个库 ID（或点下面那行辅助按钮自动填）。
        </p>
        <div class="mt-3 space-y-2">
          <el-input v-model="kbIdsInput" placeholder="kb_ids，多个用逗号分隔" size="small" />
          <el-input v-model="query" placeholder="query" size="small" />
          <div class="flex items-center gap-2">
            <el-button
              type="primary"
              :disabled="busy !== null"
              :loading="busy === 'POST /api/v1/rag/retrieve'"
              @click="retrieve"
            >
              POST /api/v1/rag/retrieve
            </el-button>
            <el-button
              link
              type="primary"
              :disabled="busy !== null"
              :loading="busy === 'GET /api/v1/knowledge-bases'"
              @click="fillKbIds"
            >
              辅助：用 dev-key 列我的知识库
            </el-button>
          </div>
        </div>
      </article>

      <article class="rounded-lg border border-line bg-white p-4 shadow-sm">
        <h2 class="text-sm font-semibold text-slate-800">3. 控制面写：登录</h2>
        <p class="mt-1 text-xs text-slate-500">
          成功后浏览器里应有 <code>console_session</code>（Path=/console、HttpOnly）。
        </p>
        <div class="mt-3 space-y-2">
          <el-input v-model="username" placeholder="账号 username" size="small" />
          <el-input
            v-model="password"
            type="password"
            show-password
            placeholder="口令 password"
            size="small"
          />
          <el-button
            type="primary"
            :disabled="busy !== null"
            :loading="busy === 'POST /console/login'"
            @click="login"
          >
            POST /console/login
          </el-button>
        </div>
      </article>

      <article class="rounded-lg border border-line bg-white p-4 shadow-sm">
        <h2 class="text-sm font-semibold text-slate-800">4. 写请求的防跨站头</h2>
        <p class="mt-1 text-xs text-slate-500">
          同一路径发两次：不带 <code>x-requested-with</code> 应得到 20011，带上应成功。
          20011 表示「没权限 / 缺头」，<strong>不是掉登录</strong>。
        </p>
        <div class="mt-3">
          <el-button
            type="primary"
            :disabled="busy !== null"
            :loading="busy === 'POST /console/logout（两组）'"
            @click="verifyWriteHeader"
          >
            验证 POST /console/logout
          </el-button>
        </div>
      </article>
    </div>

    <div class="rounded-lg border border-line bg-white">
      <div class="flex items-center justify-between border-b border-line px-4 py-2">
        <h2 class="text-sm font-semibold text-slate-800">探针结果</h2>
        <el-button size="small" :disabled="entries.length === 0" @click="clearLog">
          <el-icon class="mr-1"><Refresh /></el-icon>
          清空
        </el-button>
      </div>
      <ul v-if="entries.length > 0" class="divide-y divide-line">
        <li v-for="entry in entries" :key="entry.seq" class="px-4 py-3 text-sm">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-mono text-xs text-slate-400">#{{ entry.seq }}</span>
            <span class="font-mono text-xs text-slate-600">{{ entry.label }}</span>
            <span
              class="rounded border px-1.5 py-0.5 text-xs"
              :class="toneClass[entry.tone]"
            >
              {{ entry.tone === 'ok' ? '通过' : entry.tone === 'warn' ? '预期内' : '失败' }}
            </span>
          </div>
          <p class="mt-1 break-words text-slate-700">{{ entry.text }}</p>
        </li>
      </ul>
      <p v-else class="px-4 py-6 text-sm text-slate-400">
        还没有结果。按顺序点上面四个按钮；第 4 个会登出，所以放在登录验证之后。
      </p>
    </div>
  </section>
</template>
