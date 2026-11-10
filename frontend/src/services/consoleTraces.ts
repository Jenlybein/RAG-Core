import { consoleHttp } from '../lib/consoleHttp'
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
