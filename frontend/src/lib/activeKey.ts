/**
 * 数据面身份的唯一来源：当前选中的那把 API Key 的**原文**。
 *
 * 为什么是模块级变量而不是 Pinia：`dataHttp` 的请求拦截器不属于任何一个页面，
 * 它在「检索」这一层执行；把它挂在某个 store 上会让请求层依赖 UI 层。Pinia 里的
 * `selectedKeyId` / 原文是「用户此刻选了哪把」，这里是「请求层此刻认哪把」，
 * 两者由 `stores/retrieve.ts` 的 `selectKey` 一次性同步——只有这一个写入口。
 *
 * 三条硬约束（issue #92）：
 *
 * - **请求层这里只认内存**：这个模块不读写任何 storage，也不进 URL。调试台为了「刷新后
 *   不用重粘」另有一份 `sessionStorage`（`lib/apiKeySession`），那是一份**预填来源**，
 *   启动时由页面读出来再经 `setActiveKey` 写回这里——请求层自己永远只有一个入口。
 *   再往前那条「刷新即失效」已经按需求放宽到「同一浏览器会话内还在」：刷新还在，
 *   换一个会话不在。
 * - 这里**不做**任何鉴权判断：Key 是否有效、有没有配置，只有后端说了算。前端只负责
 *   「把用户选的那把原样带上去」；
 * - 空值即「还没选」。拦截器在空值时会抛一条可操作的错误，而不是发一个空 Bearer
 *   让后端回 20010（那会让「没人选 Key」看起来像「Key 失效」）。
 */

let activeKey: string | null = null

/** 选中 / 清空当前 Key。`null`、空串、全空白都统一成「还没选」。 */
export function setActiveKey(value: string | null): void {
  const trimmed = value === null ? '' : value.trim()
  activeKey = trimmed === '' ? null : trimmed
}

/** 当前 Key 的原文；`null` 表示还没选。请求拦截器只从这里取身份。 */
export function getActiveKey(): string | null {
  return activeKey
}
