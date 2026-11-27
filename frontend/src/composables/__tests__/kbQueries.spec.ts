/**
 * 详情查询的**纯函数**：缓存键、请求参数、关键字防抖（第 17 步 #227）。
 *
 * ## 为什么这些要单独测
 *
 * 三样东西都不依赖组件或网络，却各自有一段「说不清就出错」的边界：
 *
 * * 缓存键里塞一个响应式代理对象 → 「翻回上一页」每次都变成新请求；
 * * 空关键字传成 `''` 而不是 `undefined` → 后端把它当成筛选条件，页数与实际行数分叉；
 * * 防抖漏掉一次 `cancel` → 一次停顿连发两个请求（而且两个结果谁先回来是不确定的）。
 *
 * 这些在挂载测试里只会表现为「偶发两次请求」，定位成本极高。抽成纯函数 + 假定时器就能钉住。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createDebouncer,
  documentsQueryKey,
  documentsQueryParams,
  KEYWORD_DEBOUNCE_MS,
  PAGE_SIZE_OPTIONS,
  REAL_TIMERS,
  type DebouncerTimers,
} from '../useKbQueries'

describe('documentsQueryParams', () => {
  it('空关键字传 undefined（不是空串）', () => {
    // 后端把 `keyword=""` 当一个筛选条件；「没搜」与「搜空串」在分页上不该有差别。
    expect(
      documentsQueryParams({ page: 1, pageSize: 50, status: undefined, keyword: '   ' }),
    ).toEqual({ page: 1, pageSize: 50, status: undefined, keyword: undefined })
  })

  it('关键字去两端空白', () => {
    expect(
      documentsQueryParams({ page: 1, pageSize: 50, status: undefined, keyword: '  policy ' }),
    ).toMatchObject({ keyword: 'policy' })
  })

  it('页码下界是 1（0 与负数都当第一页）', () => {
    expect(documentsQueryParams({ page: 0, pageSize: 50, status: undefined, keyword: '' }).page).toBe(
      1,
    )
    expect(
      documentsQueryParams({ page: -3, pageSize: 50, status: undefined, keyword: '' }).page,
    ).toBe(1)
  })

  it('状态筛选原样传（1..4，含「索引中」）', () => {
    expect(
      documentsQueryParams({ page: 1, pageSize: 20, status: 3, keyword: '' }),
    ).toMatchObject({ status: 3, pageSize: 20 })
  })
})

describe('documentsQueryKey', () => {
  it('全基础值：不含对象、不含 undefined', () => {
    const key = documentsQueryKey('kb-1', {
      page: 2,
      pageSize: 50,
      status: undefined,
      keyword: undefined,
    })
    expect(key).toEqual(['knowledge-bases', 'documents', 'kb-1', 2, 50, null, ''])
    for (const part of key) {
      expect(['string', 'number', 'object'].includes(typeof part)).toBe(true)
      // `null` 是 object，但不该出现真正的对象/数组（那会破坏结构相等）。
      expect(Array.isArray(part)).toBe(false)
      expect(part === null || typeof part !== 'object').toBe(true)
    }
  })

  it('不同筛选 → 不同键；同参数两次调用 → 结构相等', () => {
    const base = { page: 1, pageSize: 50, status: undefined, keyword: undefined }
    expect(documentsQueryKey('kb-1', base)).toEqual(documentsQueryKey('kb-1', { ...base }))
    expect(documentsQueryKey('kb-1', base)).not.toEqual(
      documentsQueryKey('kb-1', { ...base, page: 2 }),
    )
    expect(documentsQueryKey('kb-1', base)).not.toEqual(
      documentsQueryKey('kb-2', base),
    )
  })

  it('kbId 为 null 时键也是稳定的（弹窗没开）', () => {
    const base = { page: 1, pageSize: 50, status: undefined, keyword: undefined }
    expect(documentsQueryKey(null, base)).toEqual(documentsQueryKey(null, { ...base }))
  })
})

describe('createDebouncer', () => {
  let pending: Array<{ fn: () => void; ms: number }>
  let cancelled: unknown[]
  let timers: DebouncerTimers

  beforeEach(() => {
    pending = []
    cancelled = []
    timers = {
      schedule: (fn, ms) => {
        pending.push({ fn, ms })
        return pending.length - 1
      },
      cancel: (handle) => {
        cancelled.push(handle)
      },
    }
  })

  it('排定一次就只是排定，不立刻执行（这就是「防抖」）', () => {
    const fn = vi.fn()
    createDebouncer(KEYWORD_DEBOUNCE_MS, timers).push(fn)
    expect(fn).not.toHaveBeenCalled()
    expect(pending).toHaveLength(1)
    expect(pending[0].ms).toBe(KEYWORD_DEBOUNCE_MS)
  })

  it('连续 push 只保留最后一次：前一次的排定被 cancel', () => {
    const debouncer = createDebouncer(KEYWORD_DEBOUNCE_MS, timers)
    const first = vi.fn()
    const second = vi.fn()
    debouncer.push(first)
    debouncer.push(second)

    expect(cancelled).toEqual([0])
    expect(pending).toHaveLength(2)

    // 只执行最后一次排定的那个（第一次已经被 cancel 掉）
    pending[1].fn()
    expect(second).toHaveBeenCalledTimes(1)
    expect(first).not.toHaveBeenCalled()
  })

  it('执行之后再 push，仍然能排定新的（handle 被清空，不会误 cancel）', () => {
    const debouncer = createDebouncer(KEYWORD_DEBOUNCE_MS, timers)
    const first = vi.fn()
    const second = vi.fn()
    debouncer.push(first)
    pending[0].fn() // 执行完
    debouncer.push(second)

    expect(cancelled).toEqual([])
    pending[1].fn()
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('默认定时器就是 setTimeout / clearTimeout（不传 timers 也能用）', () => {
    expect(REAL_TIMERS.schedule).toBeTypeOf('function')
    expect(REAL_TIMERS.cancel).toBeTypeOf('function')
    const debouncer = createDebouncer(0)
    expect(typeof debouncer.push).toBe('function')
  })
})

describe('PAGE_SIZE_OPTIONS', () => {
  it('选项都是正整数且递增（分页器的长度不能是 0）', () => {
    expect(PAGE_SIZE_OPTIONS.length).toBeGreaterThan(0)
    const sorted = [...PAGE_SIZE_OPTIONS].sort((a, b) => a - b)
    expect([...PAGE_SIZE_OPTIONS]).toEqual(sorted)
    for (const size of PAGE_SIZE_OPTIONS) expect(size).toBeGreaterThan(0)
  })
})
