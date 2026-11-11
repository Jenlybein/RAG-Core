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

/** 历史列表里的一行：与状态端点同形，外加「有没有报告可看」。 */
export interface SideEvidenceRun {
  run_id: string
  status: string
  judge_model?: string | null
  error?: string | null
  summary?: SideEvidenceStatus['summary']
  has_report: boolean
}

export interface SideEvidenceRunsPayload {
  items: SideEvidenceRun[]
  limit: number
}

/**
 * 最近若干次跑批（控制台历史）。
 *
 * 为什么要它：状态端点只能查**一个** `run_id`，而 `run_id` 是秒级时间戳、刷新页面就丢。
 * 没有列表的话，「昨天跑的那轮 CP 是多少」只能去服务器上翻目录。
 */
export function fetchSideEvidenceRuns(limit = 10): Promise<SideEvidenceRunsPayload> {
  return consoleHttp.get<SideEvidenceRunsPayload>('/console/eval/side-evidence', {
    params: { limit },
  })
}

export interface SideEvidenceReport {
  run_id: string
  /** Markdown 原文；没有报告时是 `null`（不是空串——「没有」与「空文件」要分得开）。 */
  report: string | null
}

/**
 * 读报告原文，由前端在弹窗里渲染。
 *
 * **不拼文件路径**：`report.md` 在服务端磁盘上，浏览器没有那条路径；而把 `eval/runs/` 当
 * 静态目录挂出去，等于把「按名字读任意文件」交给调用方。这条端点只认 `run_id` 形状、
 * 只读那一个固定文件名、并校验归属。
 */
export function fetchSideEvidenceReport(runId: string): Promise<SideEvidenceReport> {
  return consoleHttp.get<SideEvidenceReport>(
    `/console/eval/side-evidence/${encodeURIComponent(runId)}/report`,
  )
}
