import { isPendingDocument } from '../utils/documentStatus'
import type {
  DocumentStatusCounts,
  KbDocumentsPayload,
  KbListPayload,
  KbSummary,
} from '../types/knowledgeBase'

/**
 * 知识库页的**刷新策略**：什么时候快刷、什么时候慢查、以及界面该怎么把这件事说出来。
 *
 * ## 为什么从「全终态就停」改成「两档 + 一个活动窗」
 *
 * 老规则（本文件的前身 `useTreePolling.shouldContinuePolling`）是「还有在途工作就 3 秒一次，
 * 否则返回 `false`——彻底停」。它对省请求是对的，对**人能不能看懂**是错的：一旦停了，界面上
 * 没有任何东西说明它停了，于是「已经刷新完，数据是新的」与「停在十分钟前」长得一模一样。
 *
 * 现在三条规则：
 *
 * * **有在途文档**（有篇在排队中 / 索引中）→ 3 秒快刷（`LIVE_INTERVAL_MS`）；
 * * **最近有写入**（活动窗 `ACTIVITY_WINDOW_MS` 内刚写过）→ 也快刷：「刚写完」那一段
 *   状态变化最密集，而「刚好在那一秒停住」正是老规则的另一个坑；
 * * 否则 → 60 秒慢查（`IDLE_HEARTBEAT_MS`），不再彻底停。
 *
 * 慢查保留的理由与快刷不同：它不服务「我在等它」，它服务「**别人动了这个库**」——另一个
 * 标签页、另一个同事、清扫器重投的任务。
 *
 * ## 第 17 步·最后一批：为什么不看「构建在跑」了
 *
 * 索引产物下放到篇级之后，后端**整库构建这一层退场**（没有构建行、没有进度、没有生效版本），
 * 所以「有没有在途工作」只能从数据本身读。改用两条判据，正好各接住原来的一条：
 *
 * 1. **文档状态**（排队中 3 / 索引中 4，见 :func:`hasPendingDocuments`）：后端按篇投递，
 *    一篇从「排队中」→「索引中」→「成功/失败」都是真状态迁移，所以它就是「有东西在跑」
 *    的唯一真值来源。以前要另外维护一份构建账本才知道「这一批还剩多少」，现在那一层没了，
 *    留下的四态反而更诚实：它按篇说得出「哪一篇在跑」。
 * 2. **最近写入时刻**（:func:`recentlyWritten`）：接住原来「整库构建刚收尾」那一格。
 *    构建收尾是一瞬间的事（实测 10 篇的库 9.5 秒建完），写操作提交后行状态变「排队中」
 *    要等一笔事务落库；如果这时恰好判定为 idle，就会用 60 秒慢查盯着一个刚起跑的任务，
 *    用户看到的是「点了重建，界面一动不动」。所以留一个 30 秒窗：窗内继续每 3 秒跟几轮。
 *
 *    「写入时刻」有两个来源，取较新的那个：
 *   * 页面自己最近一次写操作的时刻（上传 / 重建 / 重交 / 回滚 / 删文档 / 改设置成功时打点，
 *     由调用方作为 `writeAt` 传进来）；
 *   * 这份数据里最新的文档落库时刻（`documents[].created_at`）：别人（另一个标签页 /
 *     另一个同事）刚上传一篇时，本页没有自己的写操作可记，但这一格同样是「刚写过」。
 *
 *    列表页只有库摘要（不读文档数组，第 17 步 #227 拆开的就是这件事），所以它只能用前者；
 *    `writeAt` 缺省 `null` 时列表页就是「没有写入时间可用」——不会误判成刚写过。
 *
 * ## 界面上必须配一句话
 *
 * 快刷/慢查都要显示「数据更新于 HH:MM:SS」，因为**一个会停的界面必须自证它停在哪一刻**。
 * 这条比把间隔调到多小重要得多。慢查档也**永远不停**（`intervalOf` 恒返回一个数字），
 * 所以「不停刷新」这条性质不依赖任何活动信号。
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
 * 「刚写完」仍然值得快刷的窗口。
 *
 * 收尾那一瞬状态变化最密集，而老规则「刚好在那一秒停住」正是另一个坑。30 秒够跟十来轮，
 * 又短到不会出现「画面里都跑完了，顶上还写着有东西在跑」的自相矛盾——第一版写 120 秒，
 * 用户当场抓到「明明都写着成功了，顶上还说在跑」，这一格就是那次留下的教训。
 */
export const ACTIVITY_WINDOW_MS = 30_000

export type RefreshMode = 'live' | 'idle'

/**
 * **为什么此刻是 live** —— 界面要按这个理由写横幅，而不是念一句写死的台词。
 *
 * `running`（真有在途文档）/ `recent`（刚写完，再跟几轮确认）/ `idle`（慢查）是三种事实，
 * 念同一句话就会出现「卡片上都写着成功，顶上还说有东西在跑」这种自相矛盾。状态对了、
 * 解释错了，比不刷新更让人不信。
 */
export type RefreshReason = 'running' | 'recent' | 'idle'

/**
 * 判断刷新档位时能拿到的两个时间信号（都由调用方给，保持本文件是**纯函数**）。
 *
 * 两个都可选：单测固定 `at` 就能把「窗口内 / 窗口外」钉住，而生产代码不传时用真实的
 * `Date.now()`。
 */
export interface RefreshSignals {
  /** 当前时刻（毫秒）。默认 `Date.now()`；单测传固定值。 */
  at?: number
  /**
   * 本页**最近一次写操作**的时刻（毫秒）。`null` / 缺省 = 这一页还没写过东西。
   *
   * 列表页的摘要里没有任何时间戳，这是它唯一的「最近写入」信号；详情页会再与
   * 文档自己的 `created_at` 取较新者（见 :func:`latestWriteAt`）。
   */
  writeAt?: number | null
}

/** 这个库有没有「还没跑完」的文档。 */
export function hasPendingDocuments(counts: DocumentStatusCounts): boolean {
  // 这两个是**篇数**不是状态值：大于 0 就是「有文档在这一态里」。
  // 判「有没有」只看计数；逐个判状态值的是详情页（`isPendingDocument`，四态的唯一来源）。
  return counts.processing > 0 || counts.queued > 0
}

/**
 * 「最近一次写入」是否还落在活动窗内。
 *
 * 判的是「离现在多久」，所以负值（服务端 / 页面时钟略快于心跳）也按「刚刚」处理——
 * 否则一次时钟漂移就会让界面掉进 idle，而它其实正在收尾。
 */
export function recentlyWritten(writeAt: number | null, at: number): boolean {
  if (writeAt === null) return false
  return at - writeAt <= ACTIVITY_WINDOW_MS
}

/**
 * 一份文档落库时刻（毫秒）。`created_at` 是**服务端**给的带时区 ISO 串，
 * 解析不出来（半截负载 / 非法值）就当「不知道」，不要用 `NaN` 去参与比较。
 */
export function documentWriteAt(row: { created_at: string | null }): number | null {
  if (row.created_at === null) return null
  const when = new Date(row.created_at).getTime()
  return Number.isNaN(when) ? null : when
}

/**
 * 这份数据里能看到的「最近写入时刻」：本页写操作时刻与文档落库时刻取较新者。
 *
 * 为什么两个都要：`writeAt` 是**本页发起的**写（上传 / 重建 / 重交…），它在线路上就已经
 * 该算「刚写过」；`created_at` 是**别人发起的**写（另一个标签页 / 另一个同事刚上传），
 * 本页没有自己的写操作可记，但列表下一轮看到它也说明「这里有活要落地」。
 */
export function latestWriteAt(
  writeAt: number | null,
  documents: readonly { created_at: string | null }[],
): number | null {
  let latest = writeAt
  for (const row of documents) {
    const when = documentWriteAt(row)
    if (when !== null && (latest === null || when > latest)) latest = when
  }
  return latest
}

/** 这个库此刻「为什么快刷」（见 :type:`RefreshReason`）。 */
export function kbRefreshReason(
  kb: KbSummary,
  signals: RefreshSignals = {},
): RefreshReason {
  const at = signals.at ?? Date.now()
  if (hasPendingDocuments(kb.status_counts)) return 'running'
  if (recentlyWritten(signals.writeAt ?? null, at)) return 'recent'
  return 'idle'
}

/** 这个库现在有没有在途工作（或刚写完、还要再跟几轮）。 */
export function kbIsLive(kb: KbSummary, signals: RefreshSignals = {}): boolean {
  return kbRefreshReason(kb, signals) !== 'idle'
}

/**
 * 列表页「为什么快刷」：任一个库在跑 → `running`；都不在跑但刚写过 → `recent`。
 *
 * 为什么把 running 优先于 recent：同时有两件事发生时，横幅该说的是**更强的那一件**
 * （「有文档在跑」比「刚写过」更需要用户知道）。
 */
export function listRefreshReason(
  payload: KbListPayload | undefined,
  signals: RefreshSignals = {},
): RefreshReason {
  if (payload === undefined) return 'idle'
  const at = signals.at ?? Date.now()
  for (const kb of payload.items) {
    const reason = kbRefreshReason(kb, signals)
    if (reason === 'running') return 'running'
  }
  // 摘要不带文档数组，列表页的「最近写入」只有本页写操作这一个来源。
  return recentlyWritten(signals.writeAt ?? null, at) ? 'recent' : 'idle'
}

/** 列表页的刷新档位（由理由派生，两者不会各自漂）。 */
export function listRefreshMode(
  payload: KbListPayload | undefined,
  signals: RefreshSignals = {},
): RefreshMode {
  return listRefreshReason(payload, signals) === 'idle' ? 'idle' : 'live'
}

/**
 * 详情页「为什么快刷」：**这个库**在途，或**这一页里**有文档在跑，或最近写过。
 *
 * 为什么不是看整个租户：详情页只对这一个库负责。别的库在跑不该让这一屏每 3 秒重取一次
 * （那样「详情页自己的节奏」就又被列表页的节奏吃掉了——这正是这次要拆开的东西）。
 */
export function documentsRefreshReason(
  payload: KbDocumentsPayload | undefined,
  signals: RefreshSignals = {},
): RefreshReason {
  if (payload === undefined) return 'idle'
  if (hasPendingDocuments(payload.kb.status_counts)) return 'running'
  for (const row of payload.documents) {
    if (isPendingDocument(row.status)) return 'running'
  }
  const at = signals.at ?? Date.now()
  const writeAt = latestWriteAt(signals.writeAt ?? null, payload.documents)
  return recentlyWritten(writeAt, at) ? 'recent' : 'idle'
}

/** 详情页的刷新档位。 */
export function documentsRefreshMode(
  payload: KbDocumentsPayload | undefined,
  signals: RefreshSignals = {},
): RefreshMode {
  return documentsRefreshReason(payload, signals) === 'idle' ? 'idle' : 'live'
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
