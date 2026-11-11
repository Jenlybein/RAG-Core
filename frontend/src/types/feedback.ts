/** 控制面「按 trace_id 反查」的响应（`GET /console/retrieval-traces`，#106）。 */
export interface TraceLookup {
  /** 归属索引那行的 id；null = 不存在或不属于当前租户（两者同义）。 */
  log_id: string | null
  trace_id: string
}

/** 数据面 `POST /api/v1/rag/feedback` 的请求体（#105）。 */
export interface FeedbackSubmission {
  log_id: string
  trace_id: string
  score: number
  comment?: string
}

/** 提交结果：回显最终分数（覆盖语义）。 */
export interface FeedbackResult {
  log_id: string
  score: number
  comment: string | null
}
