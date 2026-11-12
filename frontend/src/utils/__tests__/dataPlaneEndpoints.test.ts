import { describe, expect, it } from 'vitest'

import {
  DATA_PLANE_ENDPOINTS,
  canTrial,
  type DataPlaneEndpoint,
} from '../dataPlaneEndpoints'

/**
 * 这份清单是**契约**，所以它自己也要被钉住：
 *
 * 1. 写操作**绝不**提供试跑（真实 Key、真实数据，误点就真的改了东西）；
 * 2. 提供试跑的一定是只读的 `GET`；
 * 3. 需要额外输入的试跑（`documents` 要 kb_id）在没有输入时不给按钮。
 */
describe('dataPlaneEndpoints：接口清单的规矩', () => {
  it('写操作一个试跑都不提供', () => {
    const writers = DATA_PLANE_ENDPOINTS.filter((endpoint) => endpoint.write === true)

    expect(writers.length).toBeGreaterThan(0)
    for (const endpoint of writers) {
      expect(endpoint.trial, `${endpoint.path} 是写操作，不该有试跑`).toBeUndefined()
      // 写操作的方法也顺带钉住：只允许 POST / DELETE。
      expect(['POST', 'DELETE']).toContain(endpoint.method)
    }
  })

  it('能试跑的一定是只读 GET', () => {
    const trialable = DATA_PLANE_ENDPOINTS.filter((endpoint) => endpoint.trial !== undefined)

    expect(trialable.length).toBe(3)
    for (const endpoint of trialable) {
      expect(endpoint.method, `${endpoint.path} 提供试跑就必须是 GET`).toBe('GET')
      expect(endpoint.write).not.toBe(true)
    }
  })

  it('每条都说清了受什么限制', () => {
    for (const endpoint of DATA_PLANE_ENDPOINTS) {
      // 空字符串与「忘了写」在界面上是同一个后果：用户只能猜。
      expect(endpoint.gate.trim(), `${endpoint.path} 没写限制`).not.toBe('')
      expect(endpoint.purpose.trim(), `${endpoint.path} 没写用途`).not.toBe('')
    }
  })

  it('清单覆盖了数据面全部 9 条路由', () => {
    // 后端路由表改动时，这条会红——提醒两边一起改，而不是让清单悄悄过期。
    expect(DATA_PLANE_ENDPOINTS.map((endpoint) => `${endpoint.method} ${endpoint.path}`)).toEqual([
      'POST /rag/retrieve',
      'POST /rag/feedback',
      'GET /knowledge-bases',
      'GET /auth/me',
      'GET /documents',
      'POST /knowledge-bases/create',
      'POST /documents/upload',
      'POST /documents/{id}/reindex',
      'DELETE /documents/{id}',
    ])
  })
})

describe('canTrial：按钮该不该出现', () => {
  const documents = DATA_PLANE_ENDPOINTS.find(
    (endpoint) => endpoint.trial === 'documents',
  ) as DataPlaneEndpoint
  const authMe = DATA_PLANE_ENDPOINTS.find(
    (endpoint) => endpoint.trial === 'auth-me',
  ) as DataPlaneEndpoint
  const write = DATA_PLANE_ENDPOINTS.find(
    (endpoint) => endpoint.write === true,
  ) as DataPlaneEndpoint

  it('需要库的那条：没勾库就不给按钮', () => {
    // 「能点但必然失败」比没有按钮更糟。
    expect(canTrial(documents, { kbId: null })).toBe(false)
    expect(canTrial(documents, { kbId: '' })).toBe(false)
    expect(canTrial(documents, { kbId: 'kb-a' })).toBe(true)
  })

  it('无参的那条随时可试', () => {
    expect(canTrial(authMe, { kbId: null })).toBe(true)
  })

  it('写操作永远不给按钮', () => {
    expect(canTrial(write, { kbId: 'kb-a' })).toBe(false)
  })
})
