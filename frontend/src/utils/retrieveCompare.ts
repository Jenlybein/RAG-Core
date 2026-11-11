/**
 * 检索结果对比的纯函数：会话内历史、运行摘要 diff、结果集 diff。
 *
 * 为什么单独成文件且只收数据、不碰组件状态：对比面板要同时显示「哪些字段变了」
 * 与「哪条结果新增 / 消失 / 名次动了」，这类判定一旦写进模板就只能靠肉眼点验；
 * 抽成纯函数后，`null` 与 `0` 的区别（00-facts §5：未命中的那一路是 null，不是 0）
 * 这种最容易在模板里被 `?? 0` 悄悄抹掉的细节，才有测试能钉住。
 *
 * 每个函数都返回新对象、不原地改传入的历史：页面拿 `ref` 存历史，原地 push
 * 会让「上一次」这类引用在别处悄悄变掉，也违背「对比是会话内快照」的语义。
 */

import type { RetrievedChunk, RetrieveData, TenantPolicy } from '../types/retrieval'

/** 会话内保留的检索次数；多于这个就丢最旧的一次（issue #88 验收）。 */
export const HISTORY_LIMIT = 5

/**
 * 一次成功的检索记录。
 *
 * `seq` / `at` 由页面写入而不是在这里取 `Date.now()`：纯函数不碰时钟，测试才能复现；
 * 面板标题「与第 N 次（时间）对比」直接读这两项。失败的检索不进历史——它没有
 * `RetrieveData` 可对比，硬造一条空记录只会让「第 2 次」变成一次空白。
 */
export interface CompareHistoryEntry {
  /** 第几次检索，会话内从 1 单调递增。 */
  seq: number
  /** 这次检索的时间（ISO 字符串；本机时区在渲染时再格式化）。 */
  at: string
  data: RetrieveData
}

/**
 * 追加一次记录并裁到上限。
 *
 * FIFO：超出上限丢最旧的，保留最近几次——对比面板是「刚刚改了什么」的调试手段，
 * 不是历史报表；留太多旧结果反而让人分不清哪次是刚跑的。
 */
export function pushHistory(
  history: readonly CompareHistoryEntry[],
  entry: CompareHistoryEntry,
  limit: number = HISTORY_LIMIT,
): CompareHistoryEntry[] {
  const keep = Math.floor(limit)
  // 上限非正数等于关掉面板：不留任何一条，而不是留一条「例外」。
  if (!Number.isFinite(keep) || keep <= 0) return []

  const next = [...history, entry]
  return next.length <= keep ? next : next.slice(next.length - keep)
}

/** 摘要对比的一行：`field` 是它在响应 `metadata` 里的路径，排障时靠它对上契约。 */
export interface SummaryDiffRow {
  field: string
  /** 中文名，与 `buildRunSummary` 的标签保持同一套说法。 */
  label: string
  /** 基准那一次的值（字符串；缺省显示「—」）。 */
  before: string
  after: string
  changed: boolean
}

/** 摘要里参与比对的七个字段（父票指定）。 */
const SUMMARY_FIELDS: ReadonlyArray<{
  field: string
  label: string
  read: (data: RetrieveData) => string
}> = [
  {
    field: 'mode',
    label: '检索模式',
    read: (data) => textOf(data.metadata.mode),
  },
  {
    field: 'top_k',
    label: '返回条数',
    read: (data) => textOf(data.metadata.top_k),
  },
  {
    field: 'tenant_policy.rerank',
    label: '重排',
    read: (data) => textOf(data.metadata.tenant_policy?.rerank),
  },
  {
    field: 'tenant_policy.rewrite',
    label: '查询改写',
    read: (data) => textOf(data.metadata.tenant_policy?.rewrite),
  },
  {
    field: 'tenant_policy.clipped',
    label: '被套餐裁掉的字段',
    read: (data) => clippedText(data.metadata.tenant_policy),
  },
  {
    field: 'tenant_policy.plan',
    label: '套餐',
    read: (data) => textOf(data.metadata.tenant_policy?.plan),
  },
  {
    field: 'tenant_policy.requested_profile',
    label: '要求的档位',
    read: (data) => textOf(data.metadata.tenant_policy?.requested_profile),
  },
]

/**
 * 缺省值统一显示「—」，与检索页其它数字同一套约定（未命中 / 没有这一栏都不是 0）。
 */
function textOf(value: string | number | boolean | null | undefined): string {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

/**
 * `clipped` 是集合语义：顺序与重复都不算变化，显示时按固定顺序排好。
 *
 * 为空数组时显示「—」（等于没裁），与「整个 `tenant_policy` 缺失」在视觉上无法区分——
 * 但这两种情况下用户要做的事一样（没有东西被裁），没必要在面板上发明第三种符号。
 */
function clippedText(policy: TenantPolicy | undefined): string {
  if (policy === undefined) return '—'
  const fields = [...new Set(policy.clipped)].sort()
  return fields.length === 0 ? '—' : fields.join('、')
}

/**
 * 对比两次结果的**运行摘要**。
 *
 * 返回**全部**七个字段而不是只返回变化项：面板是两列并排，没变的行留在那里才能让人
 * 确认「这次确实没动它」；只给变化项时，「没变」与「这一字段没取到」看起来一模一样。
 * 变化与否交给 `changed`，组件只按它上色。
 */
export function diffSummary(base: RetrieveData, current: RetrieveData): SummaryDiffRow[] {
  return SUMMARY_FIELDS.map(({ field, label, read }) => {
    const before = read(base)
    const after = read(current)
    return { field, label, before, after, changed: before !== after }
  })
}

/** 结果 diff 里参与比对的字段；`rank` 是它在结果列表里的位次（第 N 名）。 */
export type ResultFieldName = 'score' | 'vector_rank' | 'bm25_rank' | 'rrf_score' | 'rank'

/**
 * 变化方向。
 *
 * 统一按「对最终排序更有利」：分数变大是 up，名次数字变小（前移）也算 up。
 * 这样面板上一律 up 用主色、down 用危险色，用户不必记住「名次是反的」。
 * `appear` / `vanish` 单独列出来，因为 null ↔ 数值不是普通的涨跌，而是「这一路
 * 这次命中 / 没命中」——00-facts §5 明确未命中的一路是 null 而不是 0。
 */
export type DiffDirection = 'up' | 'down' | 'appear' | 'vanish' | 'none'

/** 一条配对成功的结果里的单个字段对比。 */
export interface ResultFieldDiff {
  field: ResultFieldName
  label: string
  before: number | null
  after: number | null
  changed: boolean
  direction: DiffDirection
}

/** 两边都命中、但某些字段变了的一条结果。 */
export interface MatchedChunkDiff {
  /** 配对键，与 `presenceKey` 同一套规则。 */
  key: string
  chunk_id: string
  kb_id: string | null
  title: string
  fields: ResultFieldDiff[]
}

/** 只在一边出现的一条结果（新增或消失）。 */
export interface ChunkPresence {
  key: string
  chunk_id: string
  kb_id: string | null
  title: string
  /** 它在自己那一次结果里的位次，从 1 开始。 */
  rank: number
  score: number | null
}

/** `diffResults` 的输出：新增 / 消失 / 变化，外加完全一致的条数。 */
export interface ResultDiff {
  added: ChunkPresence[]
  removed: ChunkPresence[]
  changed: MatchedChunkDiff[]
  /** 两边都在且所有字段一致；面板用它说「其余 N 条无变化」。 */
  unchanged: number
}

/**
 * 配对键：`(kb_id, chunk_id)`。
 *
 * 跨库检索时不同库可能有同名切片 id，只按 `chunk_id` 配会把两条不相干的切片
 * 当成「同一条分数变了」。用 `\u0000` 分隔而不是 `:`，避免库 id 里带分隔符时
 * 拼出同一把键（`a`+`bc` 与 `ab`+`c`）。
 */
export function presenceKey(kbId: string | null, chunkId: string): string {
  return `${kbId ?? ''}\u0000${chunkId}`
}

const RESULT_FIELD_LABELS: Record<ResultFieldName, string> = {
  score: '综合排序分',
  vector_rank: '语义名次',
  bm25_rank: '关键词名次',
  rrf_score: '融合分',
  rank: '结果位次',
}

/**
 * 比一个可空数值。
 *
 * `before === after` 放在最前，所以 `null === null` 判为没变；`null` 与 `0` 走不到
 * 相等分支，只能以 `appear` / `vanish` 出现——这正是「未命中的一路不是 0 分」的保证。
 */
function diffNumber(
  field: ResultFieldName,
  before: number | null,
  after: number | null,
  higherIsBetter: boolean,
): ResultFieldDiff {
  let direction: DiffDirection
  if (before === after) {
    direction = 'none'
  } else if (before === null || before === undefined) {
    direction = 'appear'
  } else if (after === null || after === undefined) {
    direction = 'vanish'
  } else if (after > before) {
    direction = higherIsBetter ? 'up' : 'down'
  } else {
    direction = higherIsBetter ? 'down' : 'up'
  }

  return {
    field,
    label: RESULT_FIELD_LABELS[field],
    before: before ?? null,
    after: after ?? null,
    changed: before !== after,
    direction,
  }
}

/**
 * 把一次结果摊成 `key → {chunk, rank}`。
 *
 * 同一结果里同一个键理应只有一条（后端折叠后一节一条）；万一重复，后者覆盖前者，
 * 免得 diff 里出现两条同键互相比较。位次取第一次出现的位置。
 */
function indexResult(chunks: readonly RetrievedChunk[]): Map<string, { chunk: RetrievedChunk; rank: number }> {
  const index = new Map<string, { chunk: RetrievedChunk; rank: number }>()
  chunks.forEach((chunk, position) => {
    const key = presenceKey(chunk.kb_id, chunk.chunk_id)
    if (!index.has(key)) index.set(key, { chunk, rank: position + 1 })
  })
  return index
}

function presenceOf(chunk: RetrievedChunk, rank: number): ChunkPresence {
  return {
    key: presenceKey(chunk.kb_id, chunk.chunk_id),
    chunk_id: chunk.chunk_id,
    kb_id: chunk.kb_id,
    title: chunk.title,
    rank,
    score: chunk.score,
  }
}

/**
 * 对比两次结果集，按 `(kb_id, chunk_id)` 配对。
 *
 * - 只在当前出现 → `added`，位次是它在新列表里的位置；
 * - 只在基准出现 → `removed`，位次是它在旧列表里的位置；
 * - 两边都有 → 逐字段比 `score` / 两路名次 / 融合分 / 列表位次，任何一项不同才进 `changed`。
 *
 * 结果列表的顺序本身就是最终排序，所以「前 N 名 → 后 N 名」用位次表达，而不是去
 * 猜后端怎么排（融合排序依据是 `rrf_score`，见 ADR-0010）。
 */
export function diffResults(base: RetrieveData, current: RetrieveData): ResultDiff {
  const baseIndex = indexResult(base.retrieved_chunks)
  const currentIndex = indexResult(current.retrieved_chunks)

  const added: ChunkPresence[] = []
  const changed: MatchedChunkDiff[] = []
  let unchanged = 0

  current.retrieved_chunks.forEach((chunk, position) => {
    const key = presenceKey(chunk.kb_id, chunk.chunk_id)
    const before = baseIndex.get(key)
    if (before === undefined) {
      added.push(presenceOf(chunk, position + 1))
      return
    }

    const fields: ResultFieldDiff[] = [
      // 分数越大越好；名次数字越小越好（这也是 direction 统一按「更有利」的原因）。
      diffNumber('score', before.chunk.score, chunk.score, true),
      diffNumber('vector_rank', before.chunk.vector_rank, chunk.vector_rank, false),
      diffNumber('bm25_rank', before.chunk.bm25_rank, chunk.bm25_rank, false),
      diffNumber('rrf_score', before.chunk.rrf_score, chunk.rrf_score, true),
      diffNumber('rank', before.rank, position + 1, false),
    ]

    if (fields.every((field) => !field.changed)) {
      unchanged += 1
      return
    }

    changed.push({
      key,
      chunk_id: chunk.chunk_id,
      kb_id: chunk.kb_id,
      title: chunk.title,
      fields,
    })
  })

  const removed: ChunkPresence[] = []
  base.retrieved_chunks.forEach((chunk, position) => {
    const key = presenceKey(chunk.kb_id, chunk.chunk_id)
    if (!currentIndex.has(key)) removed.push(presenceOf(chunk, position + 1))
  })

  return { added, removed, changed, unchanged }
}

/**
 * 选出这次要对比的基准。
 *
 * 钉住就从历史里找那一条（可能已被 5 次上限挤掉，那就当没钉，回到「上一次」）；
 * 没钉时默认对比当前这次之前的一条，即 `history[history.length - 2]`。
 * 返回 null 表示还没有可比的对象（只跑过一次），面板据此显示提示而不是空表。
 */
export function resolveBase(
  history: readonly CompareHistoryEntry[],
  pinnedSeq: number | null,
): CompareHistoryEntry | null {
  const latest = history.length === 0 ? undefined : history[history.length - 1]
  const pinned =
    pinnedSeq === null ? undefined : history.find((entry) => entry.seq === pinnedSeq)

  // 钉住的那次若是当前这次本身，就没有东西可对比：它只能作为「下一次」的基准。
  if (pinned !== undefined && pinned !== latest) return pinned
  if (history.length < 2) return null
  return history[history.length - 2]
}

/** 供面板显示用：这一条是不是当前的（最后一次）结果。 */
export function isLatest(history: readonly CompareHistoryEntry[], seq: number): boolean {
  return history.length > 0 && history[history.length - 1].seq === seq
}
