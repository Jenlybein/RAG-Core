import { describe, expect, it } from 'vitest'

import { feedbackErrorCopy, isValidScore, scoreStars } from '../feedback'

/**
 * 评分控件的纯函数（#106）：期望值写死，不在测试里重算一遍实现。
 *
 * 三条边界值得单独钉：
 * 1. 「不属于你」与「不存在」是同一个码 20021，文案不能把它们说成两件事；
 * 2. 反馈之外的码返回空串，让通用文案接手，不能被误说成「评分没存下来」；
 * 3. 非法分数不产出星星，也不该被发到后端。
 */
describe('isValidScore', () => {
  it('只接受 1~5 的整数', () => {
    expect([1, 3, 5].every(isValidScore)).toBe(true)
    expect([0, 6, -1, 2.5, NaN].some(isValidScore)).toBe(false)
  })
})

describe('scoreStars', () => {
  it('3 分是三实心两空心', () => {
    expect(scoreStars(3)).toBe('★★★☆☆')
  })

  it('越界不产出星星', () => {
    expect(scoreStars(0)).toBe('')
    expect(scoreStars(6)).toBe('')
  })
})

describe('feedbackErrorCopy', () => {
  it('三个反馈码各给一句不同的话', () => {
    expect(feedbackErrorCopy(20020)).toBe('评分没存下来，请稍后重试')
    expect(feedbackErrorCopy(20021)).toBe('这次检索记录不存在或不属于当前账号')
    expect(feedbackErrorCopy(20022)).toBe('分数需在 1~5 之间')
  })

  it('其它码交回通用文案（返回空串）', () => {
    expect(feedbackErrorCopy(20010)).toBe('')
    expect(feedbackErrorCopy(50002)).toBe('')
  })
})
