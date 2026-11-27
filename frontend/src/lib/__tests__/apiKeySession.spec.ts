import { describe, expect, it } from 'vitest'

import {
  RETRIEVE_API_KEY_STORAGE_KEY,
  clearRetrieveApiKey,
  readRetrieveApiKey,
  writeRetrieveApiKey,
  type KeyStorageLike,
} from '../apiKeySession'

/**
 * 调试台 Key 的会话级落盘。
 *
 * 为什么用一个假 storage 而不是 jsdom：被测的是**键名契约**与「空白串当没有」这两条
 * 判定，跟浏览器实现无关。真浏览器里 `sessionStorage` 不可用时函数必须降级而不是抛错，
 * 这条也能用一个会抛的假对象钉住。
 */

function fakeStorage(initial: Record<string, string> = {}): KeyStorageLike & {
  values: Record<string, string>
} {
  const values = { ...initial }
  return {
    values,
    getItem: (key) => (key in values ? values[key] : null),
    setItem: (key, value) => {
      values[key] = value
    },
    removeItem: (key) => {
      delete values[key]
    },
  }
}

const PLAINTEXT = 'rc_live_key_1.' + 'a'.repeat(32)

describe('键名', () => {
  it('固定为 rag-console:api-key —— /keys 与 /retrieve 两端靠它对齐', () => {
    expect(RETRIEVE_API_KEY_STORAGE_KEY).toBe('rag-console:api-key')
  })
})

describe('readRetrieveApiKey', () => {
  it('有值就返回（去掉首尾空白）', () => {
    const storage = fakeStorage({ [RETRIEVE_API_KEY_STORAGE_KEY]: `  ${PLAINTEXT}  ` })
    expect(readRetrieveApiKey(storage)).toBe(PLAINTEXT)
  })

  it('没有 / 空串 / 全空白：一律 null，不返回一个会变成空 Bearer 的串', () => {
    expect(readRetrieveApiKey(fakeStorage())).toBeNull()
    expect(readRetrieveApiKey(fakeStorage({ [RETRIEVE_API_KEY_STORAGE_KEY]: '' }))).toBeNull()
    expect(readRetrieveApiKey(fakeStorage({ [RETRIEVE_API_KEY_STORAGE_KEY]: '   ' }))).toBeNull()
  })

  it('存储不可用（读取即抛）：降级成「没有预填值」，不把异常抛给页面', () => {
    const broken: KeyStorageLike = {
      getItem: () => {
        throw new Error('storage disabled')
      },
      setItem: () => undefined,
      removeItem: () => undefined,
    }
    expect(readRetrieveApiKey(broken)).toBeNull()
  })
})

describe('writeRetrieveApiKey', () => {
  it('写进去的是去空白后的原文', () => {
    const storage = fakeStorage()
    writeRetrieveApiKey(` ${PLAINTEXT} `, storage)
    expect(storage.values[RETRIEVE_API_KEY_STORAGE_KEY]).toBe(PLAINTEXT)
  })

  it('写空串 = 清空，不留下一个「看起来有值」的空字符串', () => {
    const storage = fakeStorage({ [RETRIEVE_API_KEY_STORAGE_KEY]: PLAINTEXT })
    writeRetrieveApiKey('   ', storage)
    expect(storage.values[RETRIEVE_API_KEY_STORAGE_KEY]).toBeUndefined()
  })
})

describe('clearRetrieveApiKey', () => {
  it('删掉那个键', () => {
    const storage = fakeStorage({ [RETRIEVE_API_KEY_STORAGE_KEY]: PLAINTEXT })
    clearRetrieveApiKey(storage)
    expect(storage.values[RETRIEVE_API_KEY_STORAGE_KEY]).toBeUndefined()
  })

  it('存储不可用时不抛', () => {
    const broken: KeyStorageLike = {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => {
        throw new Error('storage disabled')
      },
    }
    expect(() => clearRetrieveApiKey(broken)).not.toThrow()
  })
})
