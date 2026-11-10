import { describe, expect, it } from 'vitest'

import { describeEmptyReason, formatPercent, formatScore, formatRank } from '../format'

/**
 * 检索结果里的数字一律经过这里：分数 4 位、相似度百分比 1 位、名次「第 N 名」。
 *
 * 期望值写死成文本而不是在测试里再算一遍，否则测试只是在复述实现。
 * 「未命中的那一路是 null 而不是 0」是契约（00-facts §5），所以 null 必须显示「—」，
 * 不能用 `?? 0` 折成 0.0000——那会把「没命中」说成「命中但相关度为 0」。
 */
describe('formatScore', () => {
  it('分数保留 4 位小数', () => {
    expect(formatScore(0.0318)).toBe('0.0318')
  })

  it('真实的 0 分显示成 0.0000，与未命中区分开', () => {
    expect(formatScore(0)).toBe('0.0000')
  })

  it('未命中（null）显示破折号，不是 0', () => {
    expect(formatScore(null)).toBe('—')
  })
})

describe('formatPercent', () => {
  it('语义相似度按百分比显示，保留 1 位小数', () => {
    expect(formatPercent(0.4231)).toBe('42.3%')
  })

  it('向量这一路没命中时（null）也是破折号', () => {
    expect(formatPercent(null)).toBe('—')
  })
})

describe('formatRank', () => {
  it('名次写成「第 N 名」', () => {
    expect(formatRank(3)).toBe('第 3 名')
  })

  it('未命中的一路（null）显示破折号', () => {
    expect(formatRank(null)).toBe('—')
  })
})

/**
 * 空结果不是故障（00-facts §5），两种原因指向的下一步**完全不同**：
 * `no_indexed_chunks` 该去传文档，`no_chunks_matched` 该换问法。
 * 所以这里断言的是「两种文案互不相同」，而不是某一句字面文字——真正要守住的是
 * 两类原因不能被合并成一句「没查到相关结果」。
 */
describe('describeEmptyReason', () => {
  it('no_indexed_chunks 指向「去建索引」，与命中问题区分开', () => {
    const copy = describeEmptyReason('no_indexed_chunks')
    expect(copy.title).toContain('还没有可检索的切片')
    expect(copy.hint).toContain('上传')
  })

  it('no_chunks_matched 指向「换问法」，文案与空库不同', () => {
    const copy = describeEmptyReason('no_chunks_matched')
    expect(copy.title).toContain('没有命中')
    expect(copy.hint).toContain('问法')
    expect(copy.title).not.toBe(describeEmptyReason('no_indexed_chunks').title)
    expect(copy.hint).not.toBe(describeEmptyReason('no_indexed_chunks').hint)
  })

  it('原因缺失时也有文案，不让页面只剩空结果', () => {
    expect(describeEmptyReason(null).title).not.toBe('')
    expect(describeEmptyReason(undefined).hint).not.toBe('')
  })
})
