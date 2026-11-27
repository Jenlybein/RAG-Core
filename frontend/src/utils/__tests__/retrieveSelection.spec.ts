import { describe, expect, it } from 'vitest'

import { selectedKbSummary } from '../retrieveSelection'

/**
 * 「已选 N 个库 / 未选库」。
 *
 * 空选择必须说「未选库」而不是「已选 0 个库」：后者读起来像「选库这件事已经做过、
 * 结果是 0」，而实际上下一步正是去勾。
 */
describe('selectedKbSummary', () => {
  it('没选：未选库', () => {
    expect(selectedKbSummary(0)).toBe('未选库')
  })

  it('选了一个 / 多个：带上数字', () => {
    expect(selectedKbSummary(1)).toBe('已选 1 个库')
    expect(selectedKbSummary(6)).toBe('已选 6 个库')
  })
})
