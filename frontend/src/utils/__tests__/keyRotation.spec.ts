import { describe, expect, it } from 'vitest'

import {
  PREV_KEY_WINDOW_MS,
  formatTransitionDeadline,
  formatTransitionRemaining,
  isTransitionActive,
  prevKeyTooltip,
} from '../keyRotation'

/**
 * 过渡 Key 的显示规则。
 *
 * 为什么这些用例重要：它们守的是一条**判据**——「窗口还在不在」只看
 * `prev_key_expires_at > now`，不看 `prev_key_masked !== null`。后端提前作废时把截止时刻
 * 改到过去、却保留旧址打码形态，判据写错就会在窗口结束后继续挂图标与「作废旧 Key」，
 * 点下去只剩一句「没有可作废的过渡 Key」。所以这里把「有打码形态但窗口已关」这个最容易
 * 写错的组合单独钉住。
 *
 * 时间基准全部写成本地 naive ISO（`2026-12-01T10:00:00`）：`formatTransitionDeadline`
 * 显示的是本地时间，用带偏移的串写期望值会让用例只在本机时区对。
 */

const ACTIVE_EXPIRES = '2026-12-01T10:00:00'
const NOW = new Date('2026-12-01T07:00:00').getTime()

describe('isTransitionActive', () => {
  it('截止时刻在未来：窗口还开着', () => {
    expect(isTransitionActive(ACTIVE_EXPIRES, NOW)).toBe(true)
  })

  it('截止时刻已过去：窗口已关（哪怕 prev_key_masked 还留着）', () => {
    // 提前作废 = 把窗口关到过去；此时后端仍保留旧址末 5 位，所以这里的入参形如
    // 「有 masked、但 expiry 在昨天」——判据必须是时间比较。
    expect(isTransitionActive('2026-11-30T10:00:00', NOW)).toBe(false)
  })

  it('没有截止时刻 / 解析不了：一律算窗口不在', () => {
    expect(isTransitionActive(null, NOW)).toBe(false)
    expect(isTransitionActive('   ', NOW)).toBe(false)
    expect(isTransitionActive('not-a-date', NOW)).toBe(false)
  })

  it('恰好等于现在：算已关（窗口是开区间）', () => {
    expect(isTransitionActive('2026-12-01T07:00:00', NOW)).toBe(false)
  })
})

describe('formatTransitionDeadline', () => {
  it('本地时间按 MM-DD HH:mm 显示，不带年份', () => {
    expect(formatTransitionDeadline(ACTIVE_EXPIRES)).toBe('12-01 10:00')
  })

  it('空值与无法解析的串：不编一个假时间', () => {
    expect(formatTransitionDeadline(null)).toBe('—')
    // 解析不了就原样给出：这是用户判断窗口的唯一依据，篡改成「—」等于把证据抹掉。
    expect(formatTransitionDeadline('not-a-date')).toBe('not-a-date')
  })
})

describe('formatTransitionRemaining', () => {
  it('还剩 3 小时：显示「N 小时 M 分」', () => {
    expect(formatTransitionRemaining(ACTIVE_EXPIRES, NOW)).toBe('3 小时 0 分')
  })

  it('还剩 2 小时 30 分：两个数字都要给', () => {
    const now = new Date('2026-12-01T07:30:00').getTime()
    expect(formatTransitionRemaining(ACTIVE_EXPIRES, now)).toBe('2 小时 30 分')
  })

  it('不足 1 小时：只说「不足 1 小时」，不报精确分钟', () => {
    const now = new Date('2026-12-01T09:37:00').getTime()
    expect(formatTransitionRemaining(ACTIVE_EXPIRES, now)).toBe('不足 1 小时')
  })

  it('已到期 / 已作废：说「已过期」，不出现负数', () => {
    expect(formatTransitionRemaining(ACTIVE_EXPIRES, NOW + 4 * 60 * 60 * 1000)).toBe('已过期')
    expect(formatTransitionRemaining('2026-11-30T10:00:00', NOW)).toBe('已过期')
  })

  it('前端钟慢也不显示超过 24 小时（窗口不可能被延长）', () => {
    // 截止时刻比「现在 + 24h」还远：只可能是两边钟不同步，显示 30 小时会让人以为窗口变长了。
    const expires = '2026-12-02T13:00:00'
    expect(formatTransitionRemaining(expires, NOW)).toBe('24 小时 0 分')
    expect(PREV_KEY_WINDOW_MS).toBe(24 * 60 * 60 * 1000)
  })

  it('空值 / 解析不了：—', () => {
    expect(formatTransitionRemaining(null, NOW)).toBe('—')
    expect(formatTransitionRemaining('not-a-date', NOW)).toBe('—')
  })
})

describe('prevKeyTooltip', () => {
  it('窗口内：事实（哪把、到几点、还剩多久）+ 边界（只用于平顺切换）', () => {
    expect(prevKeyTooltip('rc_live_…00000', ACTIVE_EXPIRES, NOW)).toBe(
      '过渡 Key rc_live_…00000 仍可用，至 12-01 10:00（还剩 3 小时 0 分）。' +
        '它只用于让老业务平顺切换，过期或被作废后不再被接受。',
    )
  })

  it('不足 1 小时时把这句话原样放进去（「还剩」与它连写，不断成两截）', () => {
    const now = new Date('2026-12-01T09:50:00').getTime()
    expect(prevKeyTooltip('rc_live_…00000', ACTIVE_EXPIRES, now)).toContain('（还剩不足 1 小时）')
  })

  it('窗口已关：返回空串，调用方不会挂出一句过期文案', () => {
    // 这一条同时是「作废之后不啰嗦」的结构保证：模板与函数都只认时间。
    expect(prevKeyTooltip('rc_live_…00000', '2026-11-30T10:00:00', NOW)).toBe('')
    expect(prevKeyTooltip('rc_live_…00000', null, NOW)).toBe('')
  })
})
