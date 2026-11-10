import { describe, expect, it } from 'vitest'

import { ApiError } from '../../lib/envelope'
import type { ConsoleMe } from '../../types/console'
import { decideRedirect, navFor, probeFromError } from '../access'

/**
 * 这些用例钉的是「谁能看见哪几个入口」与「拦到哪一页」这两条产品规则，
 * 不碰组件渲染：顶栏与守卫都只是这两个纯函数的调用方。
 */

function meOf(role: string, tenantId: string | null): ConsoleMe {
  return {
    account: {
      account_id: `${role}-1`,
      username: `${role}-1`,
      role,
      status: 'active',
      tenant_id: tenantId,
    },
    tenant: null,
    plan: null,
    usage: null,
  }
}

const normalMe = meOf('user', 'tenant-a')
const adminMe = meOf('admin', null)

describe('navFor：入口按角色分流，未知身份一个入口都不给', () => {
  it('普通账号拿到用户面板的四条入口', () => {
    expect(navFor('user').map((item) => item.to)).toEqual([
      '/',
      '/knowledge-bases',
      '/keys',
      '/retrieve',
    ])
  })

  it('管理员只拿管理员入口，绝不混入用户面板的路由', () => {
    const items = navFor('admin')

    // 具体条数由 #95/#96 往 ADMIN_NAV 里补，所以这里不钉死数量；
    // 但「一条都不能是用户面板路径」是身份隔离的底线。
    expect(items.length).toBeGreaterThan(0)
    for (const item of items) {
      expect(item.to.startsWith('/admin')).toBe(true)
    }
    expect(items.map((item) => item.to)).not.toContain('/keys')
    expect(items.map((item) => item.to)).not.toContain('/retrieve')
  })

  it.each([null, undefined, '', 'admin-lite', 'guest'])(
    '未知角色 %s 不渲染任何入口',
    (role) => {
      expect(navFor(role)).toEqual([])
    },
  )

  it('管理员入口恰好是已落地的五条真实路由', () => {
    // #95 落地账号 / 套餐 / 审计，#96 补模型配置与检索参数：五条都必须有对应路由，
    // 否则顶栏点过去就是一片空白。
    expect(navFor('admin').map((item) => item.to)).toEqual([
      '/admin/accounts',
      '/admin/plans',
      '/admin/audit',
      '/admin/models',
      '/admin/retrieval',
    ])
  })
})

describe('decideRedirect：守卫的判定全在这里，页面只做执行', () => {
  it('/login 永远放行，否则登录页自己会把自己拦住', () => {
    expect(decideRedirect({ path: '/login', me: { status: 'unauthenticated' } })).toBeNull()
    expect(decideRedirect({ path: '/login', me: { status: 'unavailable' } })).toBeNull()
  })

  it('未登录（20010）访问任何业务页都跳登录页', () => {
    expect(decideRedirect({ path: '/', me: { status: 'unauthenticated' } })).toBe('/login')
    expect(decideRedirect({ path: '/retrieve', me: { status: 'unauthenticated' } })).toBe('/login')
  })

  it('后端连不上时也跳登录页，只是提示不同（拿不到业务码，按未认证兜底）', () => {
    expect(decideRedirect({ path: '/keys', me: { status: 'unavailable' } })).toBe('/login')
  })

  it('普通账号进管理员路由被拦回知识库列表', () => {
    for (const path of ['/admin/accounts', '/admin/plans', '/admin/audit']) {
      expect(decideRedirect({ path, me: { status: 'ok', me: normalMe } })).toBe('/knowledge-bases')
    }
  })

  it('管理员进用户面板路由被拦回管理员首页（管理员 tenant_id 为空，那些接口对他是 20011）', () => {
    expect(decideRedirect({ path: '/keys', me: { status: 'ok', me: adminMe } })).toBe(
      '/admin/accounts',
    )
    expect(decideRedirect({ path: '/', me: { status: 'ok', me: adminMe } })).toBe('/admin/accounts')
  })

  it('角色与路径对得上时放行（返回 null）', () => {
    expect(decideRedirect({ path: '/knowledge-bases', me: { status: 'ok', me: normalMe } })).toBeNull()
    expect(decideRedirect({ path: '/', me: { status: 'ok', me: normalMe } })).toBeNull()
    for (const path of ['/admin/accounts', '/admin/plans', '/admin/audit']) {
      expect(decideRedirect({ path, me: { status: 'ok', me: adminMe } })).toBeNull()
    }
  })

  it('20011 永不触发跳登录：那是「没权限 / 缺防跨站头」，会话还活着', () => {
    const probe = probeFromError(
      new ApiError(20011, 'console', 'missing x-requested-with header'),
    )

    expect(probe.status).toBe('forbidden')
    expect(decideRedirect({ path: '/admin/accounts', me: probe })).toBeNull()
    expect(decideRedirect({ path: '/keys', me: probe })).toBeNull()
  })
})

describe('probeFromError：把控制面失败折成守卫能读的三种结论', () => {
  it('20010 是「没会话」，跳登录', () => {
    expect(probeFromError(new ApiError(20010, 'console', 'missing console session'))).toEqual({
      status: 'unauthenticated',
    })
  })

  it('code=-1 表示没拿到业务码（连不上后端 / 非信封响应）', () => {
    expect(probeFromError(new ApiError(-1, 'console', '', '连不上后端'))).toEqual({
      status: 'unavailable',
    })
  })

  it('非 ApiError 的意外异常同样按「拿不到业务码」处理，不让页面白屏', () => {
    expect(probeFromError(new TypeError('Failed to fetch'))).toEqual({ status: 'unavailable' })
  })
})
