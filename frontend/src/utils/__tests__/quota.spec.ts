import { describe, expect, it } from 'vitest'

import type { ConsoleUsage } from '../../types/console'
import {
  NEAR_LIMIT_RATIO,
  isWordState,
  remainingOf,
  showsMeter,
  stateOf,
  usedPercentOf,
} from '../quota'

const usage = (used: number | null, limit: number): ConsoleUsage => ({
  used,
  limit,
  source: used === null ? 'not_tracked' : 'daily_counter',
  note: null,
  unit: 'calls',
})

describe('额度读数', () => {
  it('剩余 = 上限 − 已用', () => {
    expect(remainingOf(usage(190, 5000))).toBe(4810)
    expect(remainingOf(usage(0, 5000))).toBe(5000)
  })

  it('已用超过上限时剩余夹到 0，不出现负数余额', () => {
    // 并发下的计数会冲过上限（门禁是先判后加），面板不能因此显示「-3」
    expect(remainingOf(usage(5003, 5000))).toBe(0)
    expect(usedPercentOf(usage(5003, 5000))).toBe(100)
    expect(stateOf(usage(5003, 5000))).toBe('out')
  })

  it('未统计（used=null）与「已用 0」是两件事', () => {
    expect(remainingOf(usage(null, 5000))).toBeNull()
    expect(usedPercentOf(usage(null, 5000))).toBeNull()
    expect(stateOf(usage(null, 5000))).toBe('untracked')

    // 已知的 0：剩余就是全部额度，状态正常
    expect(remainingOf(usage(0, 5000))).toBe(5000)
    expect(stateOf(usage(0, 5000))).toBe('normal')
  })

  it('上限为 0（套餐不含这本账）判为未开通，不谈剩余与百分比', () => {
    expect(stateOf(usage(0, 0))).toBe('off')
    expect(remainingOf(usage(0, 0))).toBeNull()
    expect(usedPercentOf(usage(0, 0))).toBeNull()
  })

  it('接口没给这一栏（null）判为 missing，不冒充「未统计」', () => {
    // 「未统计」= 开关关着、读不到数（后端明确报了 source=not_tracked）；
    // 「没有数据」= 连这一栏都没有（后端旧版本 / 部分失败）。两者都不是 0。
    expect(stateOf(null)).toBe('missing')
    expect(remainingOf(null)).toBeNull()
    expect(isWordState('missing')).toBe(true)
    expect(showsMeter('missing')).toBe(false)
  })

  it('有读数的状态才画条；「未统计」画空槽，「未开通 / 没有数据」不画', () => {
    expect(showsMeter('normal')).toBe(true)
    expect(showsMeter('near')).toBe(true)
    expect(showsMeter('out')).toBe(true)
    expect(showsMeter('untracked')).toBe(true)
    expect(showsMeter('off')).toBe(false)
    expect(isWordState('normal')).toBe(false)
    expect(isWordState('untracked')).toBe(true)
    expect(isWordState('off')).toBe(true)
  })

  it('接近上限的分档线在 80%', () => {
    expect(NEAR_LIMIT_RATIO).toBe(0.8)
    expect(stateOf(usage(799, 1000))).toBe('normal')
    expect(stateOf(usage(800, 1000))).toBe('near')
    expect(stateOf(usage(999, 1000))).toBe('near')
    expect(stateOf(usage(1000, 1000))).toBe('out')
  })

  it('百分比是 0–100 的整数（不是 0–1 的比值），且不超过 100', () => {
    expect(usedPercentOf(usage(1, 3))).toBe(33)
    expect(usedPercentOf(usage(2, 3))).toBe(67)
    expect(usedPercentOf(usage(4, 5000))).toBe(0)
    expect(usedPercentOf(usage(9999, 5000))).toBe(100)
  })
})
