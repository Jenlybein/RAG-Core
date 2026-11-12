import { consoleHttp } from '../lib/consoleHttp'
import type { ConsoleMe } from '../types/console'

/**
 * 控制面身份接口：登录、登出、我的视图。
 *
 * 只负责「怎么请求」；能不能进页面、该跳哪一页由 `utils/access.ts` 判定、由 router 执行。
 * 三个请求都走控制面客户端（cookie + 写请求自动带防跨站头），绝不碰数据面的 Bearer 实例。
 */

/** `/console/me` 的 query key。顶栏与 T3/T4 的页面共用同一份缓存时，只有这一处定义。 */
export const CONSOLE_ME_QUERY_KEY = ['console', 'me'] as const

/** 我的账号 / 租户 / 套餐 / 用量；未登录时后端给 20010。 */
export function fetchMe(): Promise<ConsoleMe> {
  return consoleHttp.get<ConsoleMe>('/console/me')
}

/**
 * 账号 + 口令换服务端会话。成功后 cookie 由后端种下（HttpOnly），前端读不到也不需要读；
 * 失败统一 20010，秒级超限 20005——两者的下一步不同，由登录页分流。
 */
export function submitLogin(username: string, password: string): Promise<ConsoleMe> {
  return consoleHttp.post<ConsoleMe>('/console/login', { username, password })
}

/** 服务端删会话行 + 浏览器删 cookie，两半都由后端一次做完。 */
export function submitLogout(): Promise<null> {
  return consoleHttp.post<null>('/console/logout')
}
