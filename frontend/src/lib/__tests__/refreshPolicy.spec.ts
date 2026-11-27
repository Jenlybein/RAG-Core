/**
 * 刷新策略的行为用例（第 17 步 #227）。
 *
 * ## 这一票要钉住的那条规矩
 *
 * 老规则（`useTreePolling.shouldContinuePolling`）是「有在途工作就 3 秒一次，否则返回
 * `false`——彻底停」。它对省请求是对的，对**人能不能看懂**是错的：轮询一停，界面上
 * 没有任何东西说明它停了，于是「已经刷新完，数据是新的」与「停在十分钟前」长得一模一样。
 *
 * 现在的规矩是三条：在途 → 3 秒；**刚收尾（2 分钟活动窗内）→ 也 3 秒**；否则 → 60 秒慢查。
 * 这里逐条钉住，并且钉住「`intervalOf` 永远返回一个数字」——那个 `false` 再也不能回来。
 */

import { describe, expect, it } from 'vitest'

import {
  ACTIVITY_WINDOW_MS,
  buildInFlight,
  buildJustFinished,
  documentsRefreshMode,
  IDLE_HEARTBEAT_MS,
  intervalOf,
  kbIsLive,
  LIVE_INTERVAL_MS,
  listRefreshMode,
  refreshModeLabel,
  type RefreshMode,
} from '../refreshPolicy'
import type {
  BuildSummary,
  KbDocumentRow,
  KbDocumentsPayload,
  KbListPayload,
  KbSummary,
} from '../../types/knowledgeBase'

const NOW = new Date('2026-11-27T06:00:00.000Z').getTime()

function summary(overrides: Partial<KbSummary> = {}): KbSummary {
  return {
    kb_id: 'kb-1',
    name: '库',
    split_strategy: 'structure',
    allowed_formats: ['markdown'],
    chunk_size: 512,
    section_part_max_chars: 2000,
    document_count: 0,
    status_counts: { success: 0, failed: 0, processing: 0, queued: 0 },
    has_active_version: true,
    chunk_count: 0,
    needs_rebuild: false,
    token_usage: null,
    build: null,
    ...overrides,
  }
}

function build(status: string, overrides: Partial<BuildSummary> = {}): BuildSummary {
  return {
    build_id: 'b-1',
    version: 'v5',
    status,
    planned: 10,
    done: 3,
    remaining: 7,
    doc_version: 1,
    failures: [],
    failure_count: 0,
    // 一小时前：老到不该触发活动窗。
    created_at: '2026-11-27T05:00:00.000Z',
    activated_at: null,
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
    created_at: null,
    build_state: 'done',
    ...overrides,
  }
}

function documentsOf(kb: KbSummary, rows: KbDocumentRow[]): KbDocumentsPayload {
  return {
    kb,
    build: kb.build,
    documents: rows,
    total: rows.length,
    page: 1,
    page_size: 50,
    generated_at: GENERATED_AT,
  }
}

describe('buildInFlight', () => {
  it('building 与 ready 都算在途', () => {
    // `ready` 必须算：`mark_ready` 与 `activate` 是两笔事务，中间那一格（以及激活失败后
    // 长期停在 ready 的那一格）在服务端仍然会自己往前走。不算它就永远看不到它变成 active，
    // 而界面上那一格又会画成空白。
    expect(buildInFlight(build('building'))).toBe(true)
    expect(buildInFlight(build('ready'))).toBe(true)
  })

  it('收尾的三种状态都不算在途', () => {
    for (const status of ['active', 'failed', 'archived']) {
      expect(buildInFlight(build(status))).toBe(false)
    }
  })

  it('没有构建 → 不在途', () => {
    expect(buildInFlight(null)).toBe(false)
  })
})

describe('buildJustFinished', () => {
  it('生效时刻落在活动窗内 → 还要再刷几轮', () => {
    // 收尾那一瞬状态变化最密集，而老规则「刚好在那一秒停住」正是另一个坑。
    const justActivated = build('active', { activated_at: '2026-11-27T05:59:30.000Z' })
    expect(buildJustFinished(justActivated, NOW)).toBe(true)
  })

  it('没有生效时刻就退回起建时刻', () => {
    const justFinished = build('failed', { created_at: '2026-11-27T05:59:00.000Z' })
    expect(buildJustFinished(justFinished, NOW)).toBe(true)
  })

  it('早于活动窗 → 不再快刷', () => {
    const old = build('active', { activated_at: '2026-11-27T05:00:00.000Z' })
    expect(buildJustFinished(old, NOW)).toBe(false)
    expect(NOW - new Date('2026-11-27T05:00:00.000Z').getTime()).toBeGreaterThan(
      ACTIVITY_WINDOW_MS,
    )
  })

  it('两个时刻都没有 → 判不了，按「不是刚结束」处理', () => {
    expect(buildJustFinished(build('active', { created_at: null }), NOW)).toBe(false)
    expect(buildJustFinished(null, NOW)).toBe(false)
  })
})

describe('kbIsLive', () => {
  it('构建在跑 → 在途', () => {
    expect(kbIsLive(summary({ build: build('building') }), NOW)).toBe(true)
  })

  it('构建刚收尾 → 仍在活动窗内，继续快刷', () => {
    expect(
      kbIsLive(
        summary({ build: build('active', { activated_at: '2026-11-27T05:59:30.000Z' }) }),
        NOW,
      ),
    ).toBe(true)
  })

  it('构建一小时前收尾 → 不在途', () => {
    expect(kbIsLive(summary({ build: build('active') }), NOW)).toBe(false)
  })

  it('有文档排队中或索引中 → 在途', () => {
    expect(
      kbIsLive(
        summary({ status_counts: { success: 9, failed: 0, processing: 1, queued: 0 } }),
        NOW,
      ),
    ).toBe(true)
    expect(
      kbIsLive(summary({ status_counts: { success: 9, failed: 0, processing: 0, queued: 1 } }), NOW),
    ).toBe(true)
  })

  it('只有成功与失败 → 不在途', () => {
    // 失败不该触发快刷：它的状态不会自己变，盯着看只是把 API 打热。
    expect(
      kbIsLive(
        summary({ status_counts: { success: 5, failed: 3, processing: 0, queued: 0 } }),
        NOW,
      ),
    ).toBe(false)
  })
})

describe('listRefreshMode', () => {
  it('任何一个库在途 → live', () => {
    expect(
      listRefreshMode(
        listOf([summary(), summary({ kb_id: 'kb-2', build: build('building') })]),
        NOW,
      ),
    ).toBe('live')
  })

  it('全都不在途 → idle（不是「停止」）', () => {
    expect(listRefreshMode(listOf([summary(), summary({ kb_id: 'kb-2' })]), NOW)).toBe('idle')
  })

  it('还没有数据（首次加载 / 失败）→ idle', () => {
    expect(listRefreshMode(undefined, NOW)).toBe('idle')
  })
})

describe('documentsRefreshMode', () => {
  it('只看**这个库**，不看整个租户', () => {
    // 详情页只对这一个库负责：别的库在跑不该让这一屏每 3 秒重取一次，
    // 否则「详情自己的节奏」又被列表的节奏吃掉了——那正是这次要拆开的东西。
    const payload = documentsOf(summary({ kb_id: 'kb-detail' }), [row()])
    expect(documentsRefreshMode(payload, NOW)).toBe('idle')
  })

  it('这个库有构建在跑 → live', () => {
    expect(
      documentsRefreshMode(documentsOf(summary({ build: build('building') }), []), NOW),
    ).toBe('live')
  })

  it('这一页里有文档在跑 → live（即使构建状态不是 building）', () => {
    expect(
      documentsRefreshMode(
        documentsOf(summary(), [row({ status: 3, build_state: 'pending' })]),
        NOW,
      ),
    ).toBe('live')
  })

  it('排队中（4）也算在跑（四态的判定只认 documentStatus.ts 一处）', () => {
    expect(
      documentsRefreshMode(documentsOf(summary(), [row({ status: 4, build_state: 'pending' })]), NOW),
    ).toBe('live')
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

  it('活动窗比快刷间隔长得多（否则「刚收尾」那条规则等于没写）', () => {
    expect(ACTIVITY_WINDOW_MS).toBeGreaterThan(10 * LIVE_INTERVAL_MS)
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
