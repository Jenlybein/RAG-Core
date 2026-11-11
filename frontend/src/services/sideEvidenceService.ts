import { consoleHttp } from '../lib/consoleHttp'

/**
 * 语义旁证跑批的两个端点（#109）：触发一次、查它的状态。
 *
 * 走**控制面会话身份**：旁证跑的是本租户可见的题库与检索，与调用方的业务系统无关；
 * 观测服务的密钥也只留在服务端。分钟级的跑批不该挂在 HTTP 请求上，所以触发只投递任务
 * 并立刻返回 `run_id`，结论写在 `status.json` 里（由 Worker 落地）。
 */

export interface SideEvidenceStatus {
  run_id: string
  /** `queued`（任务还在队列里）/ `running` / `ok` / `partial` / `failed`。 */
  status: string
  judge_model?: string | null
  error?: string | null
  summary?: {
    context_precision?: number | null
    context_recall?: number | null
    count?: number
    scored?: number
    unscored?: number
  } | null
  report_path?: string | null
}

export interface SideEvidenceTrigger {
  run_id: string
  status: string
  note: string
}

/** 触发一次跑批；返回的 `run_id` 拿去做轮询。 */
export function triggerSideEvidence(): Promise<SideEvidenceTrigger> {
  return consoleHttp.post<SideEvidenceTrigger>('/console/eval/side-evidence')
}

/** 读一次跑批的状态与汇总（`queued` 也是成功响应）。 */
export function fetchSideEvidence(runId: string): Promise<SideEvidenceStatus> {
  return consoleHttp.get<SideEvidenceStatus>(
    `/console/eval/side-evidence/${encodeURIComponent(runId)}`,
  )
}
