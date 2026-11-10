import { describe, expect, it, vi } from 'vitest'

/**
 * 数据面身份的两条验收，在这里用**请求拦截器**本体验：
 *
 * 1. 还没选 Key 时不能发出一个没有 Bearer 的请求——那会被后端当成 20010，
 *    排障时看起来像「Key 失效」，实际只是没人选它；
 * 2. 选了 Key 之后，每一次请求头里都是那一把的原文。
 *
 * 用假的 axios 实例而不是起后端：被测的是 dataHttp 对**身份注入**的承诺，
 * 不是 axios 的网络行为。请求拦截器就是 dataHttp 的公开缝——页面与服务都不越过它。
 *
 * 拦截器在 import 期就注册好了，而 vitest 会在每个用例之间清掉 `vi.fn` 的调用记录，
 * 所以这里用普通数组接住注册进来的回调，而不是靠 `mock.calls` 事后回看。
 */
const mocks = vi.hoisted(() => {
  const requestHandlers: Array<(config: unknown) => unknown> = []
  const instance = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      request: {
        use: (handler: (config: unknown) => unknown) => {
          requestHandlers.push(handler)
          return requestHandlers.length - 1
        },
      },
      response: { use: vi.fn() },
    },
  }
  return { instance, requestHandlers }
})

vi.mock('axios', () => {
  // 只需要 create 与 isAxiosError：本用例不触发 axios 的错误分类路径。
  // 同一个对象同时给 `default` 与顶层：CJS 包在 Vite 的 interop 下，`import axios from`
  // 拿到的既可能是默认导出，也可能是命名空间对象；两种取法都指到这份假实例上。
  const fakeAxios = { create: () => mocks.instance, isAxiosError: () => false }
  return { ...fakeAxios, default: fakeAxios }
})

import { getActiveKey, setActiveKey } from '../activeKey'
import { ApiError } from '../envelope'

// import 之后 dataHttp 已经注册好拦截器；下面直接拿拦截器本体来驱动。
import { dataHttp } from '../dataHttp'

interface FakeConfig {
  headers: { set: ReturnType<typeof vi.fn> }
}

function fakeConfig(): FakeConfig {
  return { headers: { set: vi.fn() } }
}

function requestInterceptor(): (config: FakeConfig) => FakeConfig {
  const handler = mocks.requestHandlers[0]
  if (handler === undefined) throw new Error('dataHttp 没有注册请求拦截器')
  return handler as (config: FakeConfig) => FakeConfig
}

describe('dataHttp 的 Key 注入', () => {
  it('还没选 Key：抛 ApiError，文案直接告诉用户去 /keys 选一把', () => {
    setActiveKey(null)

    // 期望文案是产品承诺的一部分：只说「未认证」用户不知道该去哪。
    let caught: unknown = null
    try {
      requestInterceptor()(fakeConfig())
    } catch (error) {
      caught = error
    }
    expect(caught).toBeInstanceOf(ApiError)
    const apiError = caught as ApiError
    expect(apiError.code).toBe(-1)
    expect(apiError.message).toContain('/keys')
    expect(apiError.message).toContain('还没有选 Key')
  })

  it('选了 Key：请求头带 Bearer 原文', () => {
    setActiveKey('rc_live_0123456789abcdef')

    const config = fakeConfig()
    requestInterceptor()(config)

    expect(config.headers.set).toHaveBeenCalledTimes(1)
    expect(config.headers.set).toHaveBeenCalledWith(
      'Authorization',
      'Bearer rc_live_0123456789abcdef',
    )
  })

  it('每次请求都读**当前**的 Key：拦截器不在 import 期把值抄死', () => {
    setActiveKey('rc_live_first')
    const first = fakeConfig()
    requestInterceptor()(first)

    setActiveKey('rc_live_second')
    const second = fakeConfig()
    requestInterceptor()(second)

    expect(first.headers.set).toHaveBeenCalledWith('Authorization', 'Bearer rc_live_first')
    expect(second.headers.set).toHaveBeenCalledWith('Authorization', 'Bearer rc_live_second')
  })

  it('把「清空」交给 setActiveKey(null)：空串等价于没选，不会发一个空 Bearer', () => {
    setActiveKey('rc_live_something')
    setActiveKey('   ')

    expect(getActiveKey()).toBeNull()
    expect(() => requestInterceptor()(fakeConfig())).toThrowError(ApiError)
  })

  it('清空之后又回到「未选」：内存里的原值不会残留在下一次请求上', () => {
    setActiveKey('rc_live_temporary')
    setActiveKey(null)

    const config = fakeConfig()
    // 未选就不再写 Authorization；抛错是这条路径的正确终点。
    expect(() => requestInterceptor()(config)).toThrowError(ApiError)
    expect(config.headers.set).not.toHaveBeenCalled()
  })

  it('dataHttp 仍然只有剥壳后的五个方法，不给页面直接拿 axios 实例', () => {
    for (const method of ['get', 'post', 'put', 'patch', 'delete'] as const) {
      expect(typeof dataHttp[method]).toBe('function')
    }
  })
})
