import { describe, expect, it } from 'vitest'

import { API_KEY_HEX_LENGTH, isWellFormedApiKey, keyIdOf } from '../apiKey'

const KEY_ID = '0f8c1d6a-2b3e-4c5d-8e9f-0a1b2c3d4e5f'
const SECRET = '0123456789abcdef0123456789abcdef'

describe('keyIdOf', () => {
  it('从 rc_live_<key_id>.<32 位密钥> 里解出行身份证', () => {
    expect(keyIdOf(`rc_live_${KEY_ID}.${SECRET}`)).toBe(KEY_ID)
    // 粘贴时带上换行/空格是常事：两端空白不算错误。
    expect(keyIdOf(`  rc_live_${KEY_ID}.${SECRET}\n`)).toBe(KEY_ID)
  })

  it('形状不对一律返回 null（调用方据此提示「格式不对」）', () => {
    const bad = [
      '',
      'rc_live_…4f3a2', // 列表里的打码形态，不是可用凭证
      `rc_live_${KEY_ID}`, // 少了分隔符与密钥本体
      `rc_live_.${SECRET}`, // 少了行身份证
      `rk_live_${KEY_ID}.${SECRET}`, // 旧前缀
      `rc_live_${KEY_ID}.${SECRET.slice(0, -1)}`, // 密钥少一位
      `rc_live_${KEY_ID}.${'z'.repeat(API_KEY_HEX_LENGTH)}`, // 非十六进制
    ]
    for (const value of bad) {
      expect(keyIdOf(value), value).toBeNull()
      expect(isWellFormedApiKey(value), value).toBe(false)
    }
  })

  it('不看行身份证的格式：只回答「能不能拆开」，行在不在由后端说了算', () => {
    // 这里刻意不校验 UUID：前端多一条比后端更严的规则，只会给出一个与后端不一致的结论。
    expect(keyIdOf(`rc_live_not-a-uuid.${SECRET}`)).toBe('not-a-uuid')
  })
})
