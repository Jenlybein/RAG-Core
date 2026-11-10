import { createRouter, createWebHistory } from 'vue-router'

/**
 * T1 只挂一条真实路由：/probe 临时探针页。T2 起 `/` 换成上传页，探针页保留到 T6。
 * 其余 9 条路由留给后续票，这里不预埋空页面，免得「能点但没功能」的入口先出现。
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'upload',
      // 默认页是建库 + 上传闭环；这一条替代 T1 的 `/ → /probe` 重定向。
      component: () => import('../views/KnowledgeUploadPage.vue'),
    },
    {
      path: '/probe',
      name: 'probe',
      // 探针页自己会注明删除时间，这里不再重复。
      component: () => import('../views/ProbePage.vue'),
    },
  ],
})

export default router
