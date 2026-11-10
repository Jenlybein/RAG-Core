import 'vue-router'

/**
 * 路由 meta 的自定义字段声明。
 *
 * `layout` 决定这一页挂不挂顶栏；两个 `placeholder*` 字段只给临时占位页读，
 * 等 #84/#86/#87/#92/#95 的真实页面落地后，对应路由条目连同这两个字段一起删掉。
 */
declare module 'vue-router' {
  interface RouteMeta {
    /** `blank` = 无顶栏布局；缺省是带顶栏的默认布局。 */
    layout?: 'default' | 'blank'
    /** 占位页标题（仅供临时占位页渲染）。 */
    placeholderTitle?: string
    /** 占位页归属的票号，例如「#86 / T3」。 */
    placeholderIssue?: string
  }
}
