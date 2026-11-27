import { isPendingDocument } from '../utils/documentStatus'
import type { BuildSummary, KbDocumentsPayload, KbListPayload, KbSummary } from '../types/knowledgeBase'

/**
 * 知识库页的**刷新策略**：什么时候快刷、什么时候慢查、以及界面该怎么把这件事说出来。
 *
 * ## 为什么从「全终态就停」改成「两档 + 一个活动窗」
 *
 * 老规则（本文件的前身 `useTreePolling.shouldContinuePolling`）是「还有在途工作就 3 秒一次，
 * 否则返回 `false`——彻底停」。它对省请求是对的，对**人能不能看懂**是错的：一旦停了，界面上
 * 没有任何东西说明它停了，于是「已经刷新完，数据是新的」与「停在十分钟前」长得一模一样。
 * 实测过一次：构建 05:33:18.975 收尾，最后一次轮询 05:33:19.014 落地，之后一个请求都没有
 * ——用户盯着那屏，看到的是「重建中 3/10」永远不动。
 *
 * 现在三条规则：
 *
 * * **有在途工作**（构建在跑或在切换、有文档排队/索引中）→ 3 秒快刷（`LIVE_INTERVAL_MS`）；
 * * **最近几分钟内有活动**（刚才有构建收尾、刚上传过）→ 也快刷（`ACTIVITY_WINDOW_MS`）：
 *   收尾那一瞬的状态变化最密集，而「刚好在那一秒停住」正是老规则的另一个坑；
 * * 否则 → 60 秒慢查（`IDLE_HEARTBEAT_MS`），不再彻底停。
 *
 * 慢查保留的理由与快刷不同：它不服务「我在等它」，它服务「**别人动了这个库**」——另一个
 * 标签页、另一个同事、清扫器重投的任务。
 *
 * ## 界面上必须配一句话
 *
 * 快刷/慢查都要显示「数据更新于 HH:MM:SS」，因为**一个会停的界面必须自证它停在哪一刻**。
 * 这条比把间隔调到多小重要得多。
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

/**
 * 「刚结束的活动」仍然值得快刷的窗口。
 *
 * 构建从 `building` 收尾成 `ready → active` 是**两次**事务（`mark_ready` 先提交，`activate`
 * 另起一笔，失败还会留在 `ready`）。老规则只看文档状态与 `build_progress`，收尾那一刻两条
 * 都变成空 → 直接掉到「停止」，界面上就停在「重建中 3/10」。所以留一个窗：最近 2 分钟内
 * 有过构建行的时间戳，就继续快刷。
 */
export const ACTIVITY_WINDOW_MS = 120_000

export type RefreshMode = 'live' | 'idle'

/** 构建状态里「还在动」的那几个：`building`（在跑）与 `ready`（建好了、正在切换）。 */
const BUILD_IN_FLIGHT = new Set(['building', 'ready'])

/**
 * 这个构建是不是「还在动」。
 *
 * `ready` 必须算在内：`mark_ready` 与 `activate` 是两笔事务，中间那一格（以及激活失败后
 * 长期停在 `ready` 的那一格）在服务端仍然是可以自己往前走的——不轮询就永远看不到它变成
 * `active`，而界面上那一格又会画成空白。
 */
export function buildInFlight(build: BuildSummary | null): boolean {
  return build !== null && BUILD_IN_FLIGHT.has(build.status)
}

/** 这个构建是不是「刚结束」（收尾时间在活动窗内）。 */
export function buildJustFinished(build: BuildSummary | null, at: number): boolean {
  if (build === null) return false
  const stamp = build.activated_at ?? build.created_at
  if (stamp === null) return false
  const when = new Date(stamp).getTime()
  if (Number.isNaN(when)) return false
  return at - when <= ACTIVITY_WINDOW_MS
}

/**
 * 这个库现在有没有在途工作。
 *
 * 文档侧读的是 `isPendingDocument`（`utils/documentStatus.ts` 的**唯一**四态来源）：
 * 以前这里自己又写了一遍 `status === 3 || status === 4`，等于把「哪些状态算没跑完」
 * 拆成两处——加第五个状态时漏改一处不会报错，只会让那一批状态永远等不到刷新。
 * （列表摘要只带**四态各多少篇**，所以此处判「有没有」：`> 0`。逐个判状态值的是详情页。）
 */
export function kbIsLive(kb: KbSummary, at: number = Date.now()): boolean {
  if (buildInFlight(kb.build)) return true
  if (buildJustFinished(kb.build, at)) return true
  // 这两个是**篇数**不是状态值：大于 0 就是「有文档在这一态里」。
  return kb.status_counts.processing > 0 || kb.status_counts.queued > 0
}

/** 列表页的刷新档位。 */
export function listRefreshMode(
  payload: KbListPayload | undefined,
  at: number = Date.now(),
): RefreshMode {
  if (payload === undefined) return 'idle'
  for (const kb of payload.items) {
    if (kbIsLive(kb, at)) return 'live'
  }
  return 'idle'
}

/**
 * 详情页的刷新档位：**这个库**在途，或**这一页里**有文档在跑。
 *
 * 为什么不是看整个租户：详情页只对这一个库负责。别的库在跑不该让这一屏每 3 秒重取一次
 * （那样「详情页自己的节奏」就又被列表页的节奏吃掉了——这正是这次要拆开的东西）。
 */
export function documentsRefreshMode(
  payload: KbDocumentsPayload | undefined,
  at: number = Date.now(),
): RefreshMode {
  if (payload === undefined) return 'idle'
  if (kbIsLive(payload.kb, at)) return 'live'
  for (const row of payload.documents) {
    if (isPendingDocument(row.status)) return 'live'
  }
  // 异常分支：详情页那一份 payload 里 `build` 与 `kb.build` 是同一个值（详情一次拿全），
  // 判 kb 就够；但构建摘要单独传进来时不至于漏判。
  if (buildInFlight(payload.build) || buildJustFinished(payload.build, at)) return 'live'
  return 'idle'
}

/** 档位 → 间隔。**永远返回一个数字**：这个界面不再有「彻底停止」这一档。 */
export function intervalOf(mode: RefreshMode): number {
  return mode === 'live' ? LIVE_INTERVAL_MS : IDLE_HEARTBEAT_MS
}

/** 档位 → 给人看的一句话（界面右上角那行）。 */
export function refreshModeLabel(mode: RefreshMode): string {
  return mode === 'live'
    ? `自动刷新中（每 ${LIVE_INTERVAL_MS / 1000} 秒）`
    : `空闲，每 ${IDLE_HEARTBEAT_MS / 1000} 秒静默检查一次（数据不会自己跳到最新）`
}
