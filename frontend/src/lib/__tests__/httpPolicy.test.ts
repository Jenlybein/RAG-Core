import { describe, expect, it } from 'vitest'

import { needsWriteHeader } from '../httpPolicy'

describe('needsWriteHeader：只有会改东西的方法才需要防跨站头', () => {
  it('写方法（含 DELETE 与大小写变体）都要头', () => {
    for (const method of ['post', 'PUT', 'Patch', 'delete']) {
      expect(needsWriteHeader(method)).toBe(true)
    }
  })

  it('读方法与未声明方法不要头，避免给 GET 平白多加一个自定义头', () => {
    for (const method of ['get', 'HEAD', 'options', undefined]) {
      expect(needsWriteHeader(method)).toBe(false)
    }
  })
})
