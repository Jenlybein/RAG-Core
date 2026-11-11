import { describe, expect, it } from 'vitest'

import { ApiError, isConsoleSessionExpired, unwrapEnvelope } from '../envelope'

describe('unwrapEnvelope：业务码是唯一真相，HTTP 状态不参与判断', () => {
  it('code=0 时返回 data，原样不加工', () => {
    const data = { kb_id: 'kb-1', name: '演示库' }

    expect(unwrapEnvelope({ code: 0, msg: 'ok', data }, 'data')).toEqual(data)
  })

  it('code!==0 时抛出带 code 的 ApiError，并保留后端原文便于排障', () => {
    const error = catchError(() =>
      unwrapEnvelope({ code: 20008, msg: 'kb not visible', data: null }, 'data'),
    )

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe(20008)
    // message 是给人看的人话；后端原文另存，不能丢。
    expect(error.message).toContain('知识库不存在')
    expect(error.backendMsg).toBe('kb not visible')
  })

  it('20010 在两套身份下给出不同的下一步：数据面换 Key，控制面去登录', () => {
    const dataSide = catchError(() =>
      unwrapEnvelope({ code: 20010, msg: 'invalid api key', data: null }, 'data'),
    )
    const consoleSide = catchError(() =>
      unwrapEnvelope({ code: 20010, msg: 'missing console session', data: null }, 'console'),
    )

    expect(dataSide.message).toContain('Key 管理页')
    expect(dataSide.message).not.toContain('登录')
    expect(consoleSide.message).toContain('登录')
    expect(consoleSide.message).not.toContain('Key 管理页')
  })

  it('20011 是「没权限」，不是「掉登录」', () => {
    const error = catchError(() =>
      unwrapEnvelope({ code: 20011, msg: 'missing x-requested-with header', data: null }, 'console'),
    )

    expect(error.code).toBe(20011)
    // 文案若出现「登录」，前端或用户就会误判成会话失效并重登，而身份其实没问题。
    expect(error.message).not.toContain('登录')
    expect(isConsoleSessionExpired(20011)).toBe(false)
    expect(isConsoleSessionExpired(20010)).toBe(true)
  })

  it('不是信封（例如代理返回 HTML 或 422 detail）时抛协议错误，而不是静默当成成功', () => {
    const error = catchError(() => unwrapEnvelope('<html>Bad Gateway</html>', 'console'))

    expect(error.code).toBe(-1)
    expect(error.message).toContain('信封')
  })
})

/** 把「应该抛」的调用收成一个可断言的错误对象；抛别的类型说明契约错了。 */
function catchError(run: () => unknown): ApiError {
  try {
    run()
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('expected unwrapEnvelope to throw')
}
