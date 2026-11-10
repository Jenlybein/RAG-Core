import { describe, expect, it } from 'vitest'

import { barWidth, describeSource, toWaterfallRows, totalOf } from '../waterfall'

/**
 * 瀑布图的条宽与文案（#103）。
 *
 * 期望值写死成文本而不是在测试里再算一遍，否则测试只是在复述实现。
 *
 * 三条边界值得单独钉：
 *
 * 1. **合计为 0 时宽度是 `0%`**，不是 `NaN%` 也不是 `100%`——一段都没耗时的时候，
 *    画任何宽度都是在编数据（除零在这里必然发生，所以这条是回归点）；
 * 2. **宽度是占合计的比例**，不是占最大段的比例：88ms 在 100ms 的六段里就该是 88%，
 *    而不是「这行比别的长一点」；
 * 3. **不认识的段名不消失**：后端加了第七段或拼错名字时，界面要显示它而不是静默丢掉
 *    （观测栈那一侧会过滤，文件那一侧不会——两侧的取舍不同，界面对两者都得能显示）。
 */
describe('barWidth', () => {
  it('按占合计的比例算，保留 4 位小数', () => {
    expect(barWidth(50, 200)).toBe('25.0000%')
  })

  it('1/3 不整除时也给一个稳定的四位小数', () => {
    expect(barWidth(1, 3)).toBe('33.3333%')
  })

  it('合计为 0 时给 0%，不产生 NaN', () => {
    expect(barWidth(0, 0)).toBe('0%')
    expect(barWidth(12, 0)).toBe('0%')
  })

  it('负数按 0 处理：坏数据不该画出一条反向的条', () => {
    expect(barWidth(-1, 10)).toBe('0.0000%')
  })
})

describe('totalOf', () => {
  it('六段合计是各段之和（没发生的段不补 0，也就不在合计里）', () => {
    expect(
      totalOf([
        { name: 'query_optimization', duration_ms: 1.5 },
        { name: 'vector_recall', duration_ms: 2.5 },
      ]),
    ).toBe(4)
  })

  it('一段都没有时合计是 0', () => {
    expect(totalOf([])).toBe(0)
  })
})

describe('toWaterfallRows', () => {
  it('顺序沿用后端给的链路顺序，毫秒数保留 1 位', () => {
    const rows = toWaterfallRows([
      { name: 'rerank', duration_ms: 88.437 },
      { name: 'query_optimization', duration_ms: 11.563 },
    ])

    expect(rows.map((row) => row.name)).toEqual(['rerank', 'query_optimization'])
    expect(rows[0].ms).toBe('88.4')
    expect(rows[1].ms).toBe('11.6')
  })

  it('宽度是占合计的比例（88.437 + 11.563 = 100）', () => {
    const rows = toWaterfallRows([
      { name: 'rerank', duration_ms: 88.437 },
      { name: 'query_optimization', duration_ms: 11.563 },
    ])

    expect(rows[0].width).toBe('88.4370%')
    expect(rows[1].width).toBe('11.5630%')
  })

  it('认识的段名给中文说明，不认识的照原文留着', () => {
    const rows = toWaterfallRows([
      { name: 'vector_recall', duration_ms: 1 },
      { name: 'brand_new_stage', duration_ms: 1 },
    ])

    expect(rows[0].label).toBe('向量召回')
    expect(rows[1].name).toBe('brand_new_stage')
    expect(rows[1].label).toBe('未收录的段名')
  })
})

describe('describeSource', () => {
  it('两个来源各有中文说法，没有来源时是破折号', () => {
    expect(describeSource('jsonl')).toBe('本地记录')
    expect(describeSource('langfuse')).toBe('观测服务')
    expect(describeSource(null)).toBe('—')
  })
})
