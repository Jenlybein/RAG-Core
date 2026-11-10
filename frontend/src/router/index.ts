import { createRouter, createWebHistory } from 'vue-router'

import { fetchMe } from '../services/console'
import { decideRedirect, probeFromError, type MeProbe } from '../utils/access'

/**
 * 路由表：管理员面板的三条真实路由在本票（T13 / #95）落地 —— 账号、套餐、审计。
 *
 * `/`、`/knowledge-bases`、`/keys`、`/retrieve` 仍是占位页（各自由 #84/#86/#92/#87 替换）；
 * `/admin/models`、`/admin/retrieval` 由 #96 补。守卫与顶栏都必须能在真实路径上命中，
 * 否则「未登录跳登录页」「普通账号被拦回列表」这两条验收就没有对象可验。
 *
 * `/probe` 是 T1 的临时探针页，留着但不进任何导航，等收尾票删除。
 */
const routes = [
  {
    path: '/login',
    name: 'login',
    // 登录页不挂顶栏：顶栏要按身份渲染入口，而这一页正是「还没身份」的时候。
    meta: { layout: 'blank' as const },
    component: () => import('../views/LoginPage.vue'),
  },
  {
    path: '/',
    name: 'upload',
    meta: { placeholderTitle: '上传文档', placeholderIssue: '#84 / T2' },
    component: () => import('../views/PlaceholderPage.vue'),
  },
  {
    path: '/knowledge-bases',
    name: 'knowledge-bases',
    meta: { placeholderTitle: '知识库列表', placeholderIssue: '#86 / T3' },
    component: () => import('../views/PlaceholderPage.vue'),
  },
  {
    path: '/keys',
    name: 'keys',
    meta: { placeholderTitle: 'Key 与密钥配置', placeholderIssue: '#92 / T10' },
    component: () => import('../views/PlaceholderPage.vue'),
  },
  {
    path: '/retrieve',
    name: 'retrieve',
    meta: { placeholderTitle: '检索调试台', placeholderIssue: '#87 / T4' },
    component: () => import('../views/PlaceholderPage.vue'),
  },
  {
    path: '/admin/accounts',
    name: 'admin-accounts',
    component: () => import('../views/admin/AccountsPage.vue'),
  },
  {
    path: '/admin/plans',
    name: 'admin-plans',
    component: () => import('../views/admin/PlansPage.vue'),
  },
  {
    path: '/admin/audit',
    name: 'admin-audit',
    component: () => import('../views/admin/AuditPage.vue'),
  },
  {
    path: '/probe',
    name: 'probe',
    // T1 留下的临时探针页，本票不动它；它不进导航，收尾票自行删除。
    component: () => import('../views/ProbePage.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * 现查一次控制面身份。守卫不要猜：cookie 是 HttpOnly，前端除了问 `/console/me` 没有
 * 第二个可信来源；每个页面切换多一次读请求，对内部工具完全可以接受。
 */
async function probeMe(): Promise<MeProbe> {
  try {
    return { status: 'ok', me: await fetchMe() }
  } catch (error) {
    return probeFromError(error)
  }
}

/**
 * 路由守卫：判定全部委托给 `decideRedirect`（纯函数，有 vitest），这里只执行跳转。
 *
 * 三种落点必须分开表达，否则用户会误判原因：
 * - 20010 → 跳登录页，不提示「连不上」；
 * - 拿不到业务码 → 也跳登录页，但要带 `offline=1`，由登录页写明「后端连不上」；
 * - `20011` 等其它业务码 → 放行，**不清会话、不跳登录**（会话还活着，只是没权限）。
 */
router.beforeEach(async (to) => {
  const probe = await probeMe()
  const redirect = decideRedirect({ path: to.path, me: probe })

  if (redirect === null || redirect === to.path) return true

  return {
    path: redirect,
    // 离线标记走 query 而不是全局状态：刷新页面它也还在，不需要额外一份 Pinia 状态去同步。
    query: probe.status === 'unavailable' ? { offline: '1' } : {},
  }
})

export default router
