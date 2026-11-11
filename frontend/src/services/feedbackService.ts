import { dataHttp } from '../lib/dataHttp'
import type { FeedbackResult, FeedbackSubmission } from '../types/feedback'

/**
 * 提交打分走**数据面**（当前选中的 API Key，Bearer，#105）。
 *
 * 与检索必须同一把 Key：反馈的归属校验用的是这把 Key 解出的租户，所以「给这次检索
 * 打分」和「这次检索」不能是两把身份。反查 `log_id` 那一半在 `consoleTraces.ts`
 * （控制面会话），不在本文件：两边身份不同，混在一个文件里迟早会错用客户端。
 */
export function submitFeedback(payload: FeedbackSubmission): Promise<FeedbackResult> {
  return dataHttp.post<FeedbackResult>('/api/v1/rag/feedback', payload)
}
