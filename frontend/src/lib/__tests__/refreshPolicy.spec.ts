/**
 * 刷新策略的行为用例（第 17 步 #227）。
 *
 * ## 这一票要钉住的那条规矩
 *
 * 老规则（`useTreePolling.shouldContinuePolling`）是「有在途工作就 3 秒一次，否则返回
 * `false`——彻底停」。它对省请求是对的，对**人能不能看懂**是错的：轮询一停，界面上
 * 没有任何东西说明它停了，于是「已经刷新完，数据是新的」与「停在十分钟前」长得一模一样。
 *
 * 所以现在的规矩是两条：
 *
 * * 有在途工作 → 3 秒；
 * * 没有在途工作 → **60 秒慢查**（不再是「停止」）。
 *
 * 这里的用例就是钉住「`intervalOf` 永远返回一个数字」——那个 `false` 再也不能回来。
 */

import { describe, expect, it } from 'vitest'

import {
  documentsRefreshMode,
  IDLE_HEARTBEAT_MS,
  intervalOf,
  kbIsLive,
  LIVE_INTERVAL_MS,
  listRefreshMode,
  refreshModeLabel,
} from '../refreshPolicy'
import type { BuildSummary, KbDocumentsPayload, KbListPayload, KbSummary } from '../../types/knowledgeBase'

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
    chunk_count: 0,
    needs_rebuild: false,
    token_usage: null,
    build: null,
    ...overrides,
  }
}

function build(status: string): BuildSummary {
  return {
    build_id: 'b-1',
    version: 'v5',
    status,
    planned: 10,
    done: 3,
    pending: 7,
    doc_version: 1,
    failures: [],
    failure_count: 0,
    created_at: null,
  }
}

const GENERATED_AT = '2026-11-27T05:33:00.000Z'

function listOf(items: KbSummary[]): KbListPayload {
  return { items, generated_at: GENERATED_AT }
}

function documentsOf(kb: KbSummary, rows: KbDocumentsPayload['documents']): KbDocumentsPayload {
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

describe('kbIsLive', () => {
  it('构建在跑 → 在途', () => {
    expect(kbIsLive(summary({ build: build('building') }))).toBe(true)
  })

  it('构建已收尾（ready / active / failed）→ 不在途', () => {
    // `ready` 不算的理由：它已经收敛完，只剩一次激活，而激活与收敛在同一个事务里。
    for (const status of ['ready', 'active', 'failed', 'archived']) {
      expect(kbIsLive(summary({ build: build(status) }))).toBe(false)
    }
  })

  it('有文档排队中或索引中 → 在途', () => {
    expect(
      kbIsLive(summary({ status_counts: { success: 9, failed: 0, processing: 1, queued: 0 } })),
    ).toBe(true)
    expect(
      kbIsLive(summary({ status_counts: { success: 9, failed: 0, processing: 0, queued: 1 } })),
    ).toBe(true)
  })

  it('只有成功与失败 → 不在途', () => {
    // 失败不该触发快刷：它的状态不会自己变，盯着看只是把 API 打热。
    expect(
      kbIsLive(summary({ status_counts: { success: 5, failed: 3, processing: 0, queued: 0 } })),
    ).toBe(false)
  })
})

describe('listRefreshMode', () => {
  it('任何一个库在途 → live', () => {
    expect(listRefreshMode(listOf([summary(), summary({ kb_id: 'kb-2', build: build('building') })]))).toBe(
      'live',
    )
  })

  it('全都不在途 → idle（不是「停止」）', () => {
    expect(listRefreshMode(listOf([summary(), summary({ kb_id: 'kb-2' })]))).toBe('idle')
  })

  it('还没有数据（首次加载 / 失败）→ idle', () => {
    expect(listRefreshMode(undefined)).toBe('idle')
  })
})

describe('documentsRefreshMode', () => {
  it('只看**这个库**，不看整个租户', () => {
    // 详情页只对这一个库负责：别的库在跑不该让这一屏每 3 秒重取一次，
    // 否则「详情自己的节奏」又被列表的节奏吃掉了——那正是这次要拆开的东西。
    const payload = documentsOf(summary({ kb_id: 'kb-detail' }), [])
    expect(documentsRefreshMode(payload)).toBe('idle')
  })

  it('这个库有构建在跑 → live', () => {
    expect(documentsRefreshMode(documentsOf(summary({ build: build('building') }), []))).toBe('live')
  })

  it('这一页里有文档在跑 → live（即使构建状态不是 building）', () => {
    const payload = documentsOf(summary(), [
      {
        document_id: 'd-1',
        title: 'a.md',
        source_type: 'markdown',
        status: 3,
        chunk_count: 0,
        token_count: 0,
        error_message: null,
        current_version: 1,
        created_at: null,
        build_state: 'pending',
      },
    ])
    expect(documentsRefreshMode(payload)).toBe('live')
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
})

describe('refreshModeLabel', () => {
  it('两档各自说清「多久一次」', () => {
    expect(refreshModeLabel('live')).toContain('3 秒')
    expect(refreshModeLabel('idle')).toContain('60 秒')
  })
})
