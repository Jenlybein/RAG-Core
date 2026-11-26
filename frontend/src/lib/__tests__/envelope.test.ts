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

    // 数据面有后端原文就照用（见下一条用例），没原文才用这句通用文案。
    expect(dataSide.message).toBe('invalid api key')
    expect(dataSide.message).not.toContain('登录')
    expect(consoleSide.message).toContain('登录')
    expect(consoleSide.message).not.toContain('Key 管理页')
  })

  it('数据面 20010 照用后端原文：四种原因只有服务端知道是哪一种', () => {
    // 真实反馈：服务端说「开发身份模式（AUTH_ENABLED=false）只认占位 Key」，界面却用
    // 通用文案说「去 Key 管理页换一把」——用户于是反复重签 Key，永远修不好。
    const devMode = catchError(() =>
      unwrapEnvelope(
        {
          code: 20010,
          msg: '服务端处于开发身份模式（AUTH_ENABLED=false）：只接受内置占位 Key',
          data: null,
        },
        'data',
      ),
    )

    expect(devMode.message).toContain('AUTH_ENABLED')
    expect(devMode.message).not.toContain('Key 管理页')
  })

  it('数据面 20010 没有后端原文时仍退回通用文案，而不是空白', () => {
    const bare = catchError(() => unwrapEnvelope({ code: 20010, msg: '', data: null }, 'data'))

    expect(bare.message).toContain('Key 管理页')
  })

  it('控制面的 20010 不受后端原文影响：那个码恒等于「会话没了」', () => {
    const consoleSide = catchError(() =>
      unwrapEnvelope({ code: 20010, msg: 'invalid console session', data: null }, 'console'),
    )

    expect(consoleSide.message).toContain('登录')
  })

  it('20033 说清「问题里没有内容」并给出下一步：它不是服务故障', () => {
    // 三条模型端点（改写 / 分解 / Research）在任何模型调用之前拒掉无内容的问题。
    // 文案若只说「请求失败」，用户会去查部署、重签 Key——那种排查永远修不好。
    const error = catchError(() =>
      unwrapEnvelope(
        { code: 20033, msg: 'query has no searchable content', data: null },
        'data',
      ),
    )

    expect(error.code).toBe(20033)
    expect(error.message).toContain('没有可检索的内容')
    expect(error.message).toContain('再试')
    // 排障时仍能对回后端日志原文。
    expect(error.backendMsg).toBe('query has no searchable content')
  })

  it('20034 / 20035 各有一句中文人话：处置是「删东西或提额」，不是「等重置」', () => {
    // 未登记的码会退化成「业务码 20034」——那就等于把「库满了」换成一串数字。
    const kbCount = catchError(() =>
      unwrapEnvelope(
        { code: 20034, msg: 'knowledge base count exceeded', data: { reason: 'kb_count' } },
        'console',
      ),
    )
    const kbDocuments = catchError(() =>
      unwrapEnvelope(
        {
          code: 20035,
          msg: 'document count exceeded',
          data: { reason: 'kb_document_count' },
        },
        'console',
      ),
    )

    expect(kbCount.code).toBe(20034)
    expect(kbCount.message).toContain('知识库数量')
    expect(kbCount.message).toContain('删掉不用的知识库')
    // 库数满与文档数满是**两个码、两句话**：删的对象不同（整个库 vs 库里的旧文档）。
    expect(kbDocuments.code).toBe(20035)
    expect(kbDocuments.message).toContain('旧文档')
    expect(kbDocuments.message).not.toBe(kbCount.message)
    // 「等重置」是 20014 那一类（按天额度）的处置，容量型拒绝抄过来就是错话。
    for (const error of [kbCount, kbDocuments]) {
      expect(error.message).not.toContain('重置')
      expect(error.message).not.toContain('密钥配置')
    }
  })

  it('20014 只剩「按天调用额度」：不再提套餐上限或密钥配置', () => {
    const error = catchError(() =>
      unwrapEnvelope({ code: 20014, msg: 'daily quota exceeded', data: null }, 'data'),
    )

    expect(error.message).toContain('额度')
    expect(error.message).toContain('重置')
    // 老文案把「删旧文档就能继续」和「要运营改套餐」都塞进一句「去密钥配置里调整」，错。
    expect(error.message).not.toContain('密钥配置')
  })

  it('没登记的码退回后端原文，后端也没原文时至少露出业务码', () => {
    const withBackend = catchError(() => unwrapEnvelope({ code: 29999, msg: 'detail', data: null }, 'data'))
    const bare = catchError(() => unwrapEnvelope({ code: 29999, msg: '', data: null }, 'data'))

    expect(withBackend.message).toBe('detail')
    expect(bare.message).toContain('29999')
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
