import { createRouter, createWebHistory, type RouteLocation } from 'vue-router'

import { fetchMe } from '../services/console'
import { decideRedirect, probeFromError, type MeProbe } from '../utils/access'

/**
 * 路由表：T6 建立「能点得到」的路由，T2 把 `/` 换成真实上传页，T3 把
 * `/knowledge-bases` 换成两级树列表页，T13（#95）补齐管理员面板的账号 / 套餐 / 审计三条，
 * #96 再补模型配置与检索参数两条，#87 把 `/retrieve` 换成真实检索页，#92 把 `/keys`
 * 换成 Key 页。
 *
 * 十条路径全部落地。守卫与顶栏都必须能在这些真实路径上命中，否则「未登录跳登录页」
 * 「普通账号被拦回列表」这两条验收就没有对象可验。
 *
 * T1 的临时探针页（`/probe`）已随 #92 删除：它的两件验收物（数据面 Bearer、控制面
 * cookie + 防跨站头）现在由真实页面承担，留着它就是数据面客户端上的第二个调用点。
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
    // `/` 不再是一张独立页面：建库与上传已并进「知识库管理」。
    // 保留这条重定向是为了不打断旧链接（含带 `?kb_id=` 的深链）。
    path: '/',
    redirect: (to: RouteLocation) => ({ path: '/knowledge-bases', query: to.query }),
  },
  {
    path: '/knowledge-bases',
    name: 'knowledge-bases',
    // 知识库管理：列表 + 详情弹窗（库内文档的增删查改）+ 建库 / 上传弹窗。
    component: () => import('../views/KnowledgeManagePage.vue'),
  },
  {
    path: '/keys',
    name: 'keys',
    // #92：列 / 签发 / 编辑密钥配置 / 删 Key；`?key_id=` 直接打开那一把的配置编辑器。
    component: () => import('../views/KeysPage.vue'),
  },
  {
    path: '/retrieve',
    name: 'retrieve',
    // #87 换成了真实检索页：选 Key / 勾库 → 发检索 → 结果卡片 + 只读运行摘要。
    component: () => import('../views/RetrievePage.vue'),
  },
  {
    path: '/traces',
    name: 'traces',
    // 检索记录：本租户最近若干次检索 + 单条瀑布 + 观测栈外链。
    // 补的是「瀑布 / 评分 / 旁证都得先知道一个 id」这个缺口——在此之前只能回调用方系统里翻。
    component: () => import('../views/TracesPage.vue'),
  },
  {
    path: '/evaluation',
    name: 'evaluation',
    // 语义评测（#109 从检索页独立出来）：LLM 裁判跑 50 题，只提示、不阻断。
    // 独立成页的原因：它的结论按「轮」成立，不按「这一次检索」成立。
    component: () => import('../views/EvaluationPage.vue'),
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
    path: '/admin/models',
    name: 'admin-models',
    // #96：模型接入的增删改 + 角色指向 + 二次确认后显示一次明文 Key。
    component: () => import('../views/admin/ModelsPage.vue'),
  },
  {
    path: '/admin/retrieval',
    name: 'admin-retrieval',
    // #96：运行时配置的三档清单；只有热更 / 待重启两档给写入口。
    component: () => import('../views/admin/RetrievalPage.vue'),
  },
  {
    path: '/admin/profiles',
    name: 'admin-profiles',
    // 档位预设：四个质量档位的参数（平台级配置，改完对下一次检索立即生效）。
    component: () => import('../views/admin/ProfilesPage.vue'),
  },
  /**
   * 兜底路由：没匹配上任何一条就落到这里。
   *
   * 没有它的时候，手输一个错地址（例如 `/admin/audit-logs`，而真实路径是 `/admin/audit`）
   * 会得到一个**完全空白的 main**：顶栏还在、导航也还亮着，用户只能判断「这页坏了」，
   * 却不知道自己只是走错了门。这条路由的成本只有一个小页面，换来的是「地址写错了」
   * 与「页面挂了」能被区分开。
   *
   * 必须放在数组**最后**：Vue Router 按声明顺序匹配，通配符在前会吃掉所有真路由。
   */
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('../views/NotFoundPage.vue'),
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
