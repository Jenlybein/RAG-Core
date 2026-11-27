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
 * 另起一笔，失败还会留在 `ready`）。收尾那一瞬间文档状态与 `build_progress` 两条都变成空，
 * 所以留一个窗：最近一段时间内构建行变过，就再跟几轮。
 *
 * ## 为什么是 30 秒而不是 2 分钟
 *
 * 第一版写 120 秒，用户当场看不下去：「明明都写着**已生效**了，顶上还说有构建在跑」。
 * 这个窗的职责只有一个——接住 `ready → active` 与失败明细的最终落地，实测都是**秒级**
 * （一次 10 篇的构建：18.975 秒收敛成 ready，下一秒就 active）。30 秒够跟十来轮，又不至于
 * 让人看到一句与画面自相矛盾的话。
 */
export const ACTIVITY_WINDOW_MS = 30_000

export type RefreshMode = 'live' | 'idle'

/**
 * **为什么此刻是 live** —— 界面要按这个理由写横幅，而不是念一句写死的台词。
 *
 * 这是本轮第二次被用户抓住的同一类错：模式只有 live/idle 两档，而横幅文案写死了
 * 「有构建在跑或有文档在排队/索引中」——于是「刚收尾、正在再跟几轮」这一档也念同一句话，
 * 而画面里两个库都写着「已生效」。**状态对了、解释错了**，比不刷新更让人不信。
 *
 * 三档：`running`（真有在途工作）/ `recent`（刚收尾，再跟几轮确认）/ `idle`（慢查）。
 */
export type RefreshReason = 'running' | 'recent' | 'idle'

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

/**
 * 这个构建是不是「刚结束」（收尾时间在活动窗内）。
 *
 * 判的是「离现在多久」，所以负值（服务端时钟略快于浏览器）也按「刚刚」处理——
 * 否则一次时钟漂移就会让界面掉进 idle，而它其实正在收尾。
 */
export function buildJustFinished(build: BuildSummary | null, at: number): boolean {
  if (build === null) return false
  const stamp = build.activated_at ?? build.created_at
  if (stamp === null) return false
  const when = new Date(stamp).getTime()
  if (Number.isNaN(when)) return false
  return at - when <= ACTIVITY_WINDOW_MS
}

/** 这个库此刻「为什么快刷」（见 :type:`RefreshReason`）。 */
export function kbRefreshReason(kb: KbSummary, at: number = Date.now()): RefreshReason {
  if (buildInFlight(kb.build)) return 'running'
  // 这两个是**篇数**不是状态值：大于 0 就是「有文档在这一态里」。
  if (kb.status_counts.processing > 0 || kb.status_counts.queued > 0) return 'running'
  if (buildJustFinished(kb.build, at)) return 'recent'
  return 'idle'
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
  return kbRefreshReason(kb, at) !== 'idle'
}

/**
 * 列表页「为什么快刷」：任一个库在跑 → `running`；都不在跑但有刚收尾的 → `recent`。
 *
 * 为什么把 running 优先于 recent：同时有两件事发生时，横幅该说的是**更强的那一件**
 * （「有东西在跑」比「刚收尾」更需要用户知道）。
 */
export function listRefreshReason(
  payload: KbListPayload | undefined,
  at: number = Date.now(),
): RefreshReason {
  if (payload === undefined) return 'idle'
  let anyRecent = false
  for (const kb of payload.items) {
    const reason = kbRefreshReason(kb, at)
    if (reason === 'running') return 'running'
    if (reason === 'recent') anyRecent = true
  }
  return anyRecent ? 'recent' : 'idle'
}

/** 列表页的刷新档位（由理由派生，两者不会各自漂）。 */
export function listRefreshMode(
  payload: KbListPayload | undefined,
  at: number = Date.now(),
): RefreshMode {
  return listRefreshReason(payload, at) === 'idle' ? 'idle' : 'live'
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
