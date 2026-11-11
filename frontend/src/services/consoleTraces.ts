import { consoleHttp } from '../lib/consoleHttp'
import type { TraceLookup } from '../types/feedback'
import type { RetrievalWaterfall } from '../types/traceWaterfall'

/**
 * 控制面「按 `log_id` 读回瀑布」接口（`app/api/routes/console_traces.py`，#103）。
 *
 * 单独一个文件而不是并进 `consoleKb.ts`：`grep consoleHttp` 时，一个文件就是一组端点。
 *
 * 它走的是**会话身份**（`console_session` cookie），不是数据面的 API Key——「看这次检索
 * 的六段耗时」是研发在面板上做的事，与调用方的业务系统无关。观测服务的密钥留在服务端，
 * 本文件、本页、浏览器都拿不到，也不该拿到。
 */
export function waterfallPath(logId: string): string {
  return `/console/retrieval-traces/${encodeURIComponent(logId)}/waterfall`
}

/** 按 `log_id` 取瀑布；「未启用 / 无记录」也是成功响应（`code=0`），不是异常。 */
export function fetchWaterfall(logId: string): Promise<RetrievalWaterfall> {
  return consoleHttp.get<RetrievalWaterfall>(waterfallPath(logId))
}

/**
 * 按 `trace_id` 反查 `log_id`（`GET /console/retrieval-traces`，#106）。
 *
 * 检索响应里只有 `trace_id`，而评分与瀑布都要 `log_id`；`trace_id` 是 `log_id` 的**单向**
 * 派生，推不回去，于是这层只读反查是唯一合法入口。它走会话身份、带租户过滤，
 * 所以「查不到」同时意味着「不存在」或「不属于你」——两者不分开答。
 */
export function resolveLogId(traceId: string): Promise<TraceLookup> {
  return consoleHttp.get<TraceLookup>('/console/retrieval-traces', {
    params: { trace_id: traceId },
  })
}
