import 'vue-router'

/**
 * 路由 meta 的自定义字段声明。
 *
 * `layout` 决定这一页挂不挂顶栏。临时占位页（`PlaceholderPage.vue`）已随十条路径全部
 * 真实化而删除，只给它用的 `placeholderTitle` / `placeholderIssue` 也一并移除——
 * 留着它们会让下一个人以为还有一条占位路由在跑。
 */
declare module 'vue-router' {
  interface RouteMeta {
    /** `blank` = 无顶栏布局；缺省是带顶栏的默认布局。 */
    layout?: 'default' | 'blank'
  }
}
