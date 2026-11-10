import { ApiError } from '../lib/envelope'
import type { ConsoleMe } from '../types/console'

/**
 * 顶栏入口与路由守卫的判定，全部集中在这个纯函数模块里。
 *
 * 为什么抽出来：这两条规则（谁看得见哪些入口、谁被拦回哪一页）是产品规则，不是渲染逻辑。
 * 放进组件或 `beforeEach` 里就只能靠打开浏览器点一遍来验，而这里用 vitest 直接钉死。
 * 组件与守卫因此只剩「什么时候调用、拿到结果怎么执行」。
 */

export type RoleLike = string | null | undefined

export interface NavItem {
  readonly to: string
  readonly label: string
}

/** 用户面板的四条入口（spec #82「页面与路由」）。 */
export const USER_NAV: readonly NavItem[] = [
  { to: '/', label: '上传文档' },
  { to: '/knowledge-bases', label: '知识库列表' },
  { to: '/keys', label: 'Key 与密钥配置' },
  { to: '/retrieve', label: '检索调试台' },
]

/**
 * 管理员入口，五条真实路由：账号 / 套餐 / 审计（#95）与模型配置 / 检索参数（#96）。
 *
 * 这里只放**真实存在**的路由：顶栏 `AppHeader.vue` 直接渲染 `navFor(role)` 的结果，
 * 所以补入口只改这一处，不要在组件里再抄一份；指向一个还没实现的路由，点了就是一片
 * 空白，比没有入口更糟。
 */
export const ADMIN_NAV: readonly NavItem[] = [
  { to: '/admin/accounts', label: '账号管理' },
  { to: '/admin/plans', label: '套餐管理' },
  { to: '/admin/audit', label: '审计日志' },
  { to: '/admin/models', label: '模型配置' },
  { to: '/admin/retrieval', label: '检索参数' },
]

/**
 * 当前角色能看到的入口。未登录 / 角色不在已知取值里时返回空列表：
 * 顶栏宁可什么都不显示，也不猜一个角色去渲染别人的入口。
 */
export function navFor(role: RoleLike): readonly NavItem[] {
  if (role === 'admin') return ADMIN_NAV
  if (role === 'user') return USER_NAV
  return []
}

/** `/console/me` 的结论。守卫只认这四种状态，不需要知道 HTTP 与业务码细节。 */
export type MeProbe =
  | { status: 'ok'; me: ConsoleMe }
  /** 20010：没有会话。 */
  | { status: 'unauthenticated' }
  /** 拿不到业务码（连不上后端 / 非信封响应）。 */
  | { status: 'unavailable' }
  /** 有业务码但**不是** 20010，例如 20011：身份还活着，只是没权限或缺防跨站头。 */
  | { status: 'forbidden' }

export type RedirectTarget = '/login' | '/knowledge-bases' | '/admin/accounts'

/**
 * 守卫的完整判定：`null` 表示放行，否则返回要跳去的路径。
 *
 * 关键取舍（写在这里，免得以后有人「顺手」改掉）：
 *
 * - `20011` 返回 `null`：会话是有效的，把它当掉登录会清掉一个还能用的登录态。
 * - 管理员走用户面板路径会被拦回 `/admin/accounts`：管理员 `tenant_id` 为空，
 *   用户面板的接口对他是 `20011`，放他进去只会看到一页错误。
 * - `/login` 无条件放行，否则登录页自己会被守卫拦住，形成死循环。
 */
export function decideRedirect({ path, me }: { path: string; me: MeProbe }): RedirectTarget | null {
  if (path === '/login') return null

  if (me.status === 'unauthenticated' || me.status === 'unavailable') return '/login'
  if (me.status === 'forbidden') return null

  const isAdminPath = path === '/admin' || path.startsWith('/admin/')
  if (me.me.account.role === 'admin') {
    return isAdminPath ? null : '/admin/accounts'
  }
  return isAdminPath ? '/knowledge-bases' : null
}

/**
 * 把控制面的一次失败折成守卫能读的结论。
 *
 * 只看业务码：`20010` 才是掉登录，`-1` 是「压根没拿到业务码」，其余（20011、20002…）
 * 都说明身份没问题、只是这次请求被别的原因拒了。
 */
export function probeFromError(error: unknown): MeProbe {
  if (!(error instanceof ApiError)) return { status: 'unavailable' }
  if (error.code === 20010) return { status: 'unauthenticated' }
  if (error.code === -1) return { status: 'unavailable' }
  return { status: 'forbidden' }
}
