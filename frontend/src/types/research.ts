/**
 * `/rag/research` 的响应形状（只有调试台读的那几栏）。
 *
 * 为什么单独一个文件而不是塞进 `retrieval.ts`：Research 的 `data` 与
 * `/retrieve` 家族**不是同一个形状**——它有 `answer` / `citations` / `plan` /
 * `rounds` / `coverage`，而 `retrieved_chunks` 那一栏叫 `materials`。把两者合并成
 * 一个「都有」的超集类型，等于让调用方在两个必填栏之间二选一，类型反而更弱。
 *
 * `materials` 复用 `RetrievedChunk`：后端也是同一个值对象（ADR-0026 §2），
 * 所以「材料」与「检索结果」的字段不可能漂移。
 */

import type { RetrievedChunk } from './retrieval'

/** 一条引用块的两种状态：有证据 / 材料不足（后者下标恒为空）。 */
export type ResearchCitationStatus = 'supported' | 'insufficient'

export interface ResearchCitation {
  text: string
  /** `E1` 起编号，按最终材料顺序分配。 */
  evidence_ids: string[]
  status: ResearchCitationStatus
}

export interface ResearchPoint {
  point_id: string
  text: string
}

export interface ResearchQuery {
  point_id: string
  /** 首轮还是补证轮（1 / 2）。 */
  round: number
  query: string
}

export interface ResearchPlan {
  points: ResearchPoint[]
  queries: ResearchQuery[]
}

/** 只给计数：一共跑了几次子检索、用掉几轮补证。 */
export interface ResearchRounds {
  searches: number
  followup: number
}

export interface ResearchCoveragePoint {
  point_id: string
  /** `covered` / `partial` / `none`（词表在后端 `SUPPORT_*`）。 */
  status: string
}

export interface ResearchCoverage {
  points: ResearchCoveragePoint[]
  /** 「没覆盖到的要点」的 id，正文在 `plan.points` 里按 id 对。 */
  gaps: string[]
}

export interface ResearchMetadata {
  /** 这次调用的身份，与归属索引那一行同一个值。 */
  trace_id: string
  /** 六个词之一（`evidence_complete` / `no_new_evidence` / `max_rounds` / …）。 */
  stop_reason: string
  /** `complete` / `partial` / `missing`：材料够不够，`stop_reason` 回答不了这件事。 */
  evidence_state: string
  /** 子检索自身的降级原因（一因一码的固定串，去重）。 */
  sub_search_degraded_reasons: string[]
}

export interface ResearchData {
  query: string
  kb_ids: string[]
  /** 引用块的渲染串；四条硬校验任一不过时为 `null`（材料照给）。 */
  answer: string | null
  citations: ResearchCitation[]
  materials: RetrievedChunk[]
  plan: ResearchPlan
  rounds: ResearchRounds
  coverage: ResearchCoverage
  metadata: ResearchMetadata
}
