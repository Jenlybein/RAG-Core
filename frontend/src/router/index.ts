import { createRouter, createWebHistory } from 'vue-router'

/**
 * T1 只挂一条真实路由：/probe 临时探针页。其余 10 条路由留给后续票，
 * 这里不预埋空页面，免得「能点但没功能」的入口先出现在调试台上。
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/probe' },
    {
      path: '/probe',
      name: 'probe',
      // 探针页自己会注明删除时间，这里不再重复。
      component: () => import('../views/ProbePage.vue'),
    },
  ],
})

export default router
