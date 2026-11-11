/**
 * 检索结果与运行摘要的显示格式，集中在这里。
 *
 * 为什么要纯函数：分数位数、名次写法、空结果文案都是产品约定（01-frontend-conventions
 * 「数字与文案格式」），散在模板里就只能靠肉眼点验，且两处写法会慢慢漂移。
 * 组件只负责摆位置，不再自己 toFixed。
 */

import type { RetrievalMetadata } from '../types/retrieval'

/**
 * 分数保留 4 位。
 *
 * `null` 表示**这一路没命中**（00-facts §5：未命中的那一路是 null 而不是 0），
 * 所以显示「—」。若折成 `0.0000`，界面就把「没命中」说成了「命中但相关度为 0」，
 * 而这正是本页要帮人区分的东西。
 */
export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(4)
}

/**
 * 语义相似度按百分比显示，1 位。
 *
 * `vector_score` 是向量库给的相似度（0~1），直接摆原始小数在调试台上没法横向比较；
 * `null` 仍表示「向量这一路没命中」，与分数同一套约定。
 */
export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

/**
 * 两路名次。`vector_rank` / `bm25_rank` 为 null 表示这一路没有把这条捞上来，
 * 界面上必须与「第 0 名」区分：后者不存在，前者是常态。
 */
export function formatRank(rank: number | null | undefined): string {
  return rank === null || rank === undefined ? '—' : `第 ${rank} 名`
}

/** 空结果的文案：标题给「为什么空」，提示给「下一步做什么」。 */
export interface EmptyReasonCopy {
  title: string
  hint: string
}

/**
 * 把 `metadata.retrieval.empty_reason` 翻成两句人话（英文码本身仍在摘要里原样展示）。
 *
 * 两种原因的后续动作完全不同，合成一句「没查到相关结果」会误导：
 * - `no_indexed_chunks`：这些库根本没有可检索切片 → 先去上传文档、等索引跑完；
 * - `no_chunks_matched`：库里有内容但这一次没命中 → 换问法 / 关键词 / 换库。
 *
 * 原因缺失时也给一段兜底文案：页面不能出现「空结果但什么都不说」，
 * 那会让人以为页面卡住了。
 */
export function describeEmptyReason(reason: string | null | undefined): EmptyReasonCopy {
  if (reason === 'no_indexed_chunks') {
    return {
      title: '这些库里还没有可检索的切片',
      hint: '先在这些库里上传文档，等索引跑完再回来检索；也可以去知识库列表确认它们的索引状态。',
    }
  }
  if (reason === 'no_chunks_matched') {
    return {
      title: '库里有内容，但这次没有命中',
      hint: '换一种问法或关键词再试，或者换一个知识库；同一个库问得太具体时也容易一条都命不中。',
    }
  }
  return {
    title: '这次没有召回结果',
    hint: '响应没有给出空结果原因（empty_reason 为空）：先确认库里有已索引的切片，再换问法重试。',
  }
}

/**
 * 四个降级码的中文解释（只进 tooltip，码本身照抄英文原文）。
 *
 * 中文替身会让人拿着「向量检索失败」去搜日志却搜不到；所以界面上必须同时出现
 * `vector search failed` 原文和这句解释，而不是用中文把原文盖掉。
 */
const DEGRADED_REASON_HINTS: Record<string, string> = {
  'vector search failed': '向量召回这一路失败：这次只用关键词一路的结果，语义相似度与向量名次会缺失。',
  'keyword search failed': '关键词召回这一路失败：这次只用向量一路的结果，关键词匹配分与关键词名次会缺失。',
  'rerank failed': '重排失败：已回落到融合排序，名次可能不如预期；结果本身仍然可用。',
  'query rewrite failed': '查询改写失败：这次用原句检索，没有用改写后的问句。',
}

export function degradedReasonHint(reason: string | null | undefined): string | null {
  if (reason === null || reason === undefined) return null
  return (
    DEGRADED_REASON_HINTS[reason] ?? '后端报告了未登记的降级原因：按这行原文与后端日志排查。'
  )
}

/** 运行摘要的一行：值只来自响应，`field` 是它在 `metadata` 里的路径。 */
export interface SummaryRow {
  /** 中文名，给读的人看。 */
  label: string
  /** 响应字段路径；排障时靠它和契约对上，也证明这个值不是从页面输入来的。 */
  field: string
  /** 原样值（布尔也照抄 true/false，不翻译成第二套真相）；null 显示「—」。 */
  value: string
  /** 值的中文解释（tooltip）；没有就 null。 */
  hint: string | null
}

/**
 * 把响应 `metadata` 摊成只读摘要的若干行。
 *
 * 这是「摘要不看页面输入」唯一可以写成测试的地方：函数只收 `RetrievalMetadata`，
 * 拿不到 query / 勾选状态 / 输入框里的任何东西，所以只要它产出正确，页面就不可能
 * 拿页面配置冒充运行事实。
 *
 * 只有根 `metadata.mode` 参与展示：`metadata.retrieval.mode` 是后端解析后的同名字段，
 * 两个都摆出来只会让人问「为什么有两行 mode、哪个算」。
 */
export function buildRunSummary(metadata: RetrievalMetadata): SummaryRow[] {
  const block = metadata.retrieval
  const rows: SummaryRow[] = [
    { label: '检索模式', field: 'mode', value: metadata.mode, hint: null },
    { label: '返回条数', field: 'top_k', value: String(metadata.top_k), hint: null },
    { label: '向量库', field: 'vector_store', value: metadata.vector_store, hint: null },
    { label: '是否降级', field: 'retrieval.degraded', value: String(block.degraded), hint: null },
    {
      label: '降级原因',
      field: 'retrieval.degraded_reason',
      value: block.degraded_reason ?? '—',
      hint: degradedReasonHint(block.degraded_reason),
    },
    {
      label: '空结果原因',
      field: 'retrieval.empty_reason',
      value: block.empty_reason ?? '—',
      // 有结果时这里是 null，不该硬塞一段 tooltip 解释「空结果」。
      hint: block.empty_reason === null ? null : describeEmptyReason(block.empty_reason).hint,
    },
  ]

  const policy = metadata.tenant_policy
  if (policy === undefined) return rows

  rows.push(
    { label: '套餐', field: 'tenant_policy.plan', value: policy.plan, hint: null },
    {
      label: '要求的档位',
      field: 'tenant_policy.requested_profile',
      value: policy.requested_profile,
      hint: null,
    },
    { label: '重排', field: 'tenant_policy.rerank', value: String(policy.rerank), hint: null },
    { label: '查询改写', field: 'tenant_policy.rewrite', value: String(policy.rewrite), hint: null },
    { label: '本次库数', field: 'tenant_policy.kb_count', value: String(policy.kb_count), hint: null },
    {
      label: '被套餐裁掉的字段',
      field: 'tenant_policy.clipped',
      value: policy.clipped.length === 0 ? '—' : policy.clipped.join('、'),
      hint: policy.clipped.length === 0 ? '没有被套餐上限裁掉任何字段。' : null,
    },
  )
  return rows
}
