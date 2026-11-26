import { describe, expect, it } from 'vitest'

import {
  DOCUMENT_STATUS,
  isDocumentBusy,
  isPendingDocument,
} from '../documentStatus'

/**
 * 四态的两个判定（`isPendingDocument` / `isDocumentBusy`）是「加一态」这件事最容易被
 * 写漏的地方：它们分别回答「还要不要刷新」与「能不能操作」，而这两个问题的答案**不同**。
 *
 * 最要守住的一条：排队中还没跑完（要继续轮询），但**可以操作**（删 / 重交 / 回滚）。
 * 若有人把它们并成一个判断，排队就变回囚笼——那正是这次要修的东西。
 */

describe('DOCUMENT_STATUS', () => {
  it('取值与后端 DocumentStatus 一致', () => {
    expect(DOCUMENT_STATUS.SUCCESS).toBe(1)
    expect(DOCUMENT_STATUS.FAILED).toBe(2)
    expect(DOCUMENT_STATUS.PROCESSING).toBe(3)
    expect(DOCUMENT_STATUS.QUEUED).toBe(4)
  })
})

describe('isPendingDocument', () => {
  it('排队中与索引中都还没跑完', () => {
    expect(isPendingDocument(DOCUMENT_STATUS.QUEUED)).toBe(true)
    expect(isPendingDocument(DOCUMENT_STATUS.PROCESSING)).toBe(true)
  })

  it('成功与失败是终态', () => {
    expect(isPendingDocument(DOCUMENT_STATUS.SUCCESS)).toBe(false)
    expect(isPendingDocument(DOCUMENT_STATUS.FAILED)).toBe(false)
  })

  it('未知值不猜：不把它当在途（不为一个看不懂的状态一直轮询）', () => {
    expect(isPendingDocument(99)).toBe(false)
  })
})

describe('isDocumentBusy', () => {
  it('只有索引中可能有人正在写这篇的切片', () => {
    expect(isDocumentBusy(DOCUMENT_STATUS.PROCESSING)).toBe(true)
  })

  it('排队中不忙：删除 / 重建 / 回滚都该给按钮（排队不是囚笼）', () => {
    expect(isDocumentBusy(DOCUMENT_STATUS.QUEUED)).toBe(false)
    expect(isDocumentBusy(DOCUMENT_STATUS.SUCCESS)).toBe(false)
    expect(isDocumentBusy(DOCUMENT_STATUS.FAILED)).toBe(false)
  })
})
