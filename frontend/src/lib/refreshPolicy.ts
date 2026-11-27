import type { KbDocumentsPayload, KbListPayload, KbSummary } from '../types/knowledgeBase'

/**
 * 知识库页的**刷新策略**：什么时候快刷、什么时候慢查、以及界面该怎么把这件事说出来。
 *
 * ## 为什么从「全终态就停」改成「两档」
 *
 * 老规则（`shouldContinuePolling`）是「还有在途工作就 3 秒一次，否则 `false`——彻底停」。
 * 它对「省请求」是对的，对「人能不能看懂」是错的：一旦停了，界面**没有任何东西**说明它停
 * 了，于是「停了」与「卡住了」在屏幕上一模一样。实测过一次：构建 18.975 秒收尾，最后一次
 * 轮询 19.014 秒落地，之后一个请求都没有——用户盯着那屏，看到的是「重建中 3/10」永远不动。
 *
 * 现在两条规则：
 *
 * * **有在途工作**（构建在跑、或有文档在排队/索引中）→ 3 秒快刷（`LIVE_INTERVAL_MS`）；
 * * **没有在途工作** → 60 秒慢查（`IDLE_HEARTBEAT_MS`），不再彻底停。
 *
 * 慢查保留的理由与快刷不同：它不服务「我在等它」，它服务「**别人动了这个库**」——另一个
 * 标签页、另一个同事、清扫器重投的任务。一分钟内自己发现，比让人按 F5 强。
 *
 * ## 界面上必须配一句话
 *
 * 快刷/慢查都要显示「数据更新于 HH:MM:SS」，因为**一个会停的界面必须自证它停在哪一刻**。
 * 这条比间隔调到多小重要得多。
 */

/** 有在途工作时的间隔：够快，又不至于把 API 打成忙等。 */
export const LIVE_INTERVAL_MS = 3000

/**
 * 空闲时的慢查间隔。
 *
 * 60 秒是「人还能接受、服务器也不心疼」的交点：单库几千篇的租户，列表接口是一次按库分组的
 * 聚合，一分钟一次可以忽略；而对「另一个标签页刚点了重建」这种场景，一分钟内自己发现已经比
 * 「必须 F5」好一个量级。
 */
export const IDLE_HEARTBEAT_MS = 60_000

export type RefreshMode = 'live' | 'idle'

/** 这个库现在有没有在途工作（构建在跑，或有文档排队中/索引中）。 */
export function kbIsLive(kb: KbSummary): boolean {
  // `ready` 不算：它已经收敛完，只剩一次激活，不值得每 3 秒去问（激活在同一个事务里）。
  if (kb.build !== null && kb.build.status === 'building') return true
  return kb.status_counts.processing > 0 || kb.status_counts.queued > 0
}

/** 列表页的刷新档位。 */
export function listRefreshMode(payload: KbListPayload | undefined): RefreshMode {
  if (payload === undefined) return 'idle'
  return payload.items.some(kbIsLive) ? 'live' : 'idle'
}

/**
 * 详情页的刷新档位：**这个库**在途，或**这一页里**有文档在跑。
 *
 * 为什么不是看整个租户：详情页只对这一个库负责。别的库在跑不该让这一屏每 3 秒重取一次
 * （那样「详情页自己的节奏」就又被列表页的节奏吃掉了——这正是这次要拆开的东西）。
 */
export function documentsRefreshMode(payload: KbDocumentsPayload | undefined): RefreshMode {
  if (payload === undefined) return 'idle'
  if (kbIsLive(payload.kb)) return 'live'
  return payload.documents.some(
    (row) => row.status === DOCUMENT_STATUS_PROCESSING || row.status === DOCUMENT_STATUS_QUEUED,
  )
    ? 'live'
    : 'idle'
}

/** 与 `app/utils/status.py` 的四态同源（这里只用来判「还在跑」）。 */
export const DOCUMENT_STATUS_PROCESSING = 3
export const DOCUMENT_STATUS_QUEUED = 4

/** 档位 → 间隔。**永远返回一个数字**：这个界面不再有「彻底停止」这一档。 */
export function intervalOf(mode: RefreshMode): number {
  return mode === 'live' ? LIVE_INTERVAL_MS : IDLE_HEARTBEAT_MS
}

/** 档位 → 给人看的一句话（界面右上角那行）。 */
export function refreshModeLabel(mode: RefreshMode): string {
  return mode === 'live'
    ? `自动刷新中（每 ${LIVE_INTERVAL_MS / 1000} 秒）`
    : `空闲（每 ${IDLE_HEARTBEAT_MS / 1000} 秒静默检查一次）`
}
