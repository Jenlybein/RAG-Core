/**
 * 刷新策略的行为用例（第 17 步 #227 / 最后一批改判据）。
 *
 * ## 这一票要钉住的两条规矩
 *
 * 1. **不停刷新**：老规则是「有在途工作就 3 秒一次，否则返回 `false`——彻底停」。它对省请求
 *    是对的，对**人能不能看懂**是错的：轮询一停，界面上没有任何东西说明它停了，于是
 *    「已经刷新完，数据是新的」与「停在十分钟前」长得一模一样。现在 `intervalOf` 永远返回
 *    一个数字，慢查档是 60 秒——那个 `false` 再也不能回来。
 * 2. **判据只说数据里有的东西**：构建层退场后，「有在途工作」只能从文档四态（排队中/索引中）
 *    与「最近写入时刻」读出来，不再有任何构建行可看。这里逐条钉住这两个信号。
 */

import { describe, expect, it } from 'vitest'

import {
  ACTIVITY_WINDOW_MS,
  documentWriteAt,
  documentsRefreshMode,
  documentsRefreshReason,
  hasPendingDocuments,
  IDLE_HEARTBEAT_MS,
  intervalOf,
  kbIsLive,
  kbRefreshReason,
  latestWriteAt,
  LIVE_INTERVAL_MS,
  listRefreshMode,
  listRefreshReason,
  recentlyWritten,
  refreshModeLabel,
  type RefreshMode,
} from '../refreshPolicy'
import type {
  DocumentStatusCounts,
  KbDocumentRow,
  KbDocumentsPayload,
  KbListPayload,
  KbSummary,
} from '../../types/knowledgeBase'

const NOW = new Date('2026-11-27T06:00:00.000Z').getTime()

function counts(overrides: Partial<DocumentStatusCounts> = {}): DocumentStatusCounts {
  return { success: 0, failed: 0, processing: 0, queued: 0, ...overrides }
}

function summary(overrides: Partial<KbSummary> = {}): KbSummary {
  return {
    kb_id: 'kb-1',
    name: '库',
    split_strategy: 'structure',
    allowed_formats: ['markdown'],
    chunk_size: 512,
    section_part_max_chars: 2000,
    document_count: 0,
    status_counts: counts(),
    chunk_count: 0,
    stale_documents: 0,
    token_usage: null,
    ...overrides,
  }
}

const GENERATED_AT = '2026-11-27T05:33:00.000Z'

function listOf(items: KbSummary[]): KbListPayload {
  return { items, generated_at: GENERATED_AT }
}

function row(overrides: Partial<KbDocumentRow> = {}): KbDocumentRow {
  return {
    document_id: 'd-1',
    title: 'a.md',
    source_type: 'markdown',
    status: 1,
    chunk_count: 0,
    token_count: 0,
    error_message: null,
    current_version: 1,
    created_at: '2026-11-20T00:00:00.000Z',
    ...overrides,
  }
}

function documentsOf(kb: KbSummary, rows: KbDocumentRow[]): KbDocumentsPayload {
  return {
    kb,
    documents: rows,
    total: rows.length,
    page: 1,
    page_size: 50,
    generated_at: GENERATED_AT,
  }
}

describe('hasPendingDocuments（在途工作的唯一数据判据）', () => {
  it('有篇在排队或索引中 → 有在途工作', () => {
    // 4 = 排队中（还没被 Worker 接走），3 = 索引中（有人正在写这篇的切片）。
    expect(hasPendingDocuments(counts({ queued: 1 }))).toBe(true)
    expect(hasPendingDocuments(counts({ processing: 1 }))).toBe(true)
    expect(hasPendingDocuments(counts({ queued: 3, processing: 2 }))).toBe(true)
  })

  it('只有成功与失败 → 没有在途工作', () => {
    // 失败不该触发快刷：它的状态不会自己变，盯着看只是把 API 打热。
    expect(hasPendingDocuments(counts({ success: 5, failed: 3 }))).toBe(false)
    expect(hasPendingDocuments(counts())).toBe(false)
  })
})

describe('recentlyWritten / latestWriteAt（最近写入时刻）', () => {
  it('落在活动窗内 → 还要再刷几轮', () => {
    // 写操作提交后行状态变「排队中」要等一笔事务；这时若判定为 idle，就会用 60 秒慢查
    // 盯着一个刚起跑的任务，用户看到的是「点了重建，界面一动不动」。
    const justWrote = NOW - 5_000
    expect(recentlyWritten(justWrote, NOW)).toBe(true)
    expect(recentlyWritten(NOW - ACTIVITY_WINDOW_MS, NOW)).toBe(true)
  })

  it('早于活动窗 → 不再快刷', () => {
    expect(recentlyWritten(NOW - ACTIVITY_WINDOW_MS - 1, NOW)).toBe(false)
    expect(recentlyWritten(NOW - 10 * 60_000, NOW)).toBe(false)
  })

  it('没有写入时刻 → 判不了，按「不是刚写过」处理', () => {
    expect(recentlyWritten(null, NOW)).toBe(false)
  })

  it('负值（时钟略快于心跳）也按「刚刚」处理', () => {
    // 一次时钟漂移不该把正在收尾的界面推进 idle。
    expect(recentlyWritten(NOW + 2_000, NOW)).toBe(true)
  })

  it('文档的 created_at 能解析成毫秒；空/非法值当「不知道」', () => {
    expect(documentWriteAt({ created_at: '2026-11-27T05:59:30.000Z' })).toBe(
      new Date('2026-11-27T05:59:30.000Z').getTime(),
    )
    expect(documentWriteAt({ created_at: null })).toBeNull()
    expect(documentWriteAt({ created_at: '不是时间' })).toBeNull()
  })

  it('latestWriteAt 取「本页写操作」与「文档落库」中较新的那一个', () => {
    // 两个来源各管一类写：本页自己发起的（上传/重建…）与别人发起的（另一标签页刚上传）。
    const pageWrite = NOW - 5_000
    const newerDoc = row({ created_at: '2026-11-27T05:59:58.000Z' })
    const latest = latestWriteAt(pageWrite, [newerDoc])
    expect(latest).not.toBeNull()
    expect(latest!).toBeGreaterThan(pageWrite)

    const olderDocs = [row({ created_at: '2026-11-01T00:00:00.000Z' })]
    expect(latestWriteAt(pageWrite, olderDocs)).toBe(pageWrite)

    expect(latestWriteAt(null, [])).toBeNull()
    expect(latestWriteAt(null, [row({ created_at: null })])).toBeNull()
  })
})

describe('kbRefreshReason / kbIsLive', () => {
  it('有文档在跑 → running（不管写没写过）', () => {
    expect(kbRefreshReason(summary({ status_counts: counts({ processing: 1 }) }), { at: NOW })).toBe(
      'running',
    )
    expect(kbRefreshReason(summary({ status_counts: counts({ queued: 1 }) }), { at: NOW })).toBe(
      'running',
    )
  })

  it('刚写过 → recent（不是 running）', () => {
    // 这是用户看到「卡片都写着成功、顶上却说有东西在跑」的那一格：状态是对的
    // （活动窗内确实要再跟几轮），错的是把它说成 running。
    const justWrote = summary()
    expect(kbRefreshReason(justWrote, { at: NOW, writeAt: NOW - 5_000 })).toBe('recent')
    expect(kbIsLive(justWrote, { at: NOW, writeAt: NOW - 5_000 })).toBe(true)
  })

  it('写操作过了活动窗 → idle', () => {
    expect(kbRefreshReason(summary(), { at: NOW, writeAt: NOW - 60_000 })).toBe('idle')
    expect(kbIsLive(summary(), { at: NOW, writeAt: NOW - 60_000 })).toBe(false)
  })

  it('没写过也没在跑 → idle（但仍会每 60 秒慢查，不是停止）', () => {
    expect(kbRefreshReason(summary(), { at: NOW })).toBe('idle')
    expect(kbIsLive(summary(), { at: NOW })).toBe(false)
  })
})

describe('listRefreshReason / listRefreshMode', () => {
  it('任何一个库在跑 → live（running 优先于 recent）', () => {
    // 同时有两件事发生时，横幅该说的是**更强的那一件**。
    expect(
      listRefreshReason(
        listOf([summary({ kb_id: 'kb-a' }), summary({ kb_id: 'kb-2', status_counts: counts({ queued: 1 }) })]),
        { at: NOW, writeAt: NOW - 5_000 },
      ),
    ).toBe('running')
    expect(
      listRefreshMode(
        listOf([summary({ kb_id: 'kb-2', status_counts: counts({ processing: 1 }) })]),
        { at: NOW },
      ),
    ).toBe('live')
  })

  it('都不在跑但刚写过 → recent，且仍是 live', () => {
    // 列表摘要只带四态计数、不带文档数组，所以它的「最近写入」只有本页写操作这一个来源。
    const items = listOf([summary({ kb_id: 'kb-a' }), summary({ kb_id: 'kb-b' })])
    expect(listRefreshReason(items, { at: NOW, writeAt: NOW - 3_000 })).toBe('recent')
    expect(listRefreshMode(items, { at: NOW, writeAt: NOW - 3_000 })).toBe('live')
  })

  it('都没在跑也没写过 → idle（横幅不出现）', () => {
    expect(
      listRefreshReason(listOf([summary(), summary({ kb_id: 'kb-2' })]), { at: NOW }),
    ).toBe('idle')
    expect(listRefreshReason(undefined, { at: NOW })).toBe('idle')
  })

  it('活动窗是 30 秒：过了窗就回 idle，不能一直说「在跑」', () => {
    // 第一版写 120 秒，用户当场抓到「明明都写着成功了，顶上还说在跑」。
    expect(ACTIVITY_WINDOW_MS).toBe(30_000)
    expect(
      kbRefreshReason(summary(), { at: NOW + 30_001, writeAt: NOW }),
    ).toBe('idle')
  })
})

describe('documentsRefreshReason / documentsRefreshMode', () => {
  it('只看**这个库**，不看整个租户', () => {
    // 详情页只对这一个库负责：别的库在跑不该让这一屏每 3 秒重取一次，
    // 否则「详情自己的节奏」又被列表的节奏吃掉了——那正是这次要拆开的东西。
    const payload = documentsOf(summary({ kb_id: 'kb-detail' }), [row()])
    expect(documentsRefreshMode(payload, { at: NOW })).toBe('idle')
  })

  it('这个库的摘要说有文档在跑 → live', () => {
    expect(
      documentsRefreshMode(
        documentsOf(summary({ status_counts: counts({ queued: 1 }) }), []),
        { at: NOW },
      ),
    ).toBe('live')
  })

  it('这一页里有文档在跑 → live（即使摘要计数还是旧的）', () => {
    expect(
      documentsRefreshReason(documentsOf(summary(), [row({ status: 3 })]), { at: NOW }),
    ).toBe('running')
    // 排队中（4）也算在跑（四态的判定只认 documentStatus.ts 一处）。
    expect(
      documentsRefreshReason(documentsOf(summary(), [row({ status: 4 })]), { at: NOW }),
    ).toBe('running')
  })

  it('这份数据里最新的文档是刚落的 → recent（接住「刚上传」那一格）', () => {
    const payload = documentsOf(summary(), [
      row({ status: 1, created_at: '2026-11-20T00:00:00.000Z' }),
      // 5 秒前刚上传的一篇：本页可能没点过上传（别人传的），但它意味着有活要落地。
      row({ document_id: 'd-2', status: 1, created_at: '2026-11-27T05:59:55.000Z' }),
    ])
    expect(documentsRefreshReason(payload, { at: NOW })).toBe('recent')
    expect(documentsRefreshMode(payload, { at: NOW })).toBe('live')
  })

  it('什么都没动 → idle', () => {
    const payload = documentsOf(summary(), [row({ created_at: '2026-11-01T00:00:00.000Z' })])
    expect(documentsRefreshReason(payload, { at: NOW })).toBe('idle')
    expect(documentsRefreshReason(undefined, { at: NOW })).toBe('idle')
  })
})

describe('intervalOf', () => {
  it('**永远返回一个数字**：这个界面不再有「彻底停止」这一档', () => {
    // 反例就是老实现：`false` 让 TanStack Query 停掉轮询，而界面上没有任何东西
    // 说明它停了——「停了」与「卡住了」因此长得一模一样。
    expect(intervalOf('live')).toBe(LIVE_INTERVAL_MS)
    expect(intervalOf('idle')).toBe(IDLE_HEARTBEAT_MS)
    expect(typeof intervalOf('idle')).toBe('number')
  })

  it('空闲慢查慢于快刷，且量级差在「十秒」以上', () => {
    expect(IDLE_HEARTBEAT_MS).toBeGreaterThan(LIVE_INTERVAL_MS)
    expect(IDLE_HEARTBEAT_MS).toBeGreaterThanOrEqual(10 * LIVE_INTERVAL_MS)
  })

  it('活动窗至少容得下十来轮快刷（否则「刚写过」那条规则等于没写）', () => {
    expect(ACTIVITY_WINDOW_MS).toBeGreaterThanOrEqual(10 * LIVE_INTERVAL_MS)
  })
})

describe('refreshModeLabel', () => {
  it('两档各自说清「多久一次」，空闲那档要说清它不是实时', () => {
    const live: RefreshMode = 'live'
    const idle: RefreshMode = 'idle'
    expect(refreshModeLabel(live)).toContain('3 秒')
    expect(refreshModeLabel(idle)).toContain('60 秒')
    // 光写「每 60 秒」不够：读的人要知道这屏不会自己跳到最新。
    expect(refreshModeLabel(idle)).toContain('不会自己跳到最新')
  })
})
