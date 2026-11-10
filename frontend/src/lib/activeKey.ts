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
 * - **只进内存**：不写 `localStorage` / `sessionStorage`，不进 URL。刷新即失效，
 *   这是有意的：一把长期有效的明文凭据不该因为「上次选过」而自动复活；
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
