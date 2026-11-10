import { dataHttp } from '../lib/dataHttp'
import type { RetrieveData, RetrieveRequest } from '../types/retrieval'

/**
 * 发一次检索。
 *
 * **这是全仓库唯一的数据面调用点**（`POST /api/v1/rag/retrieve`）。登录 / 我的 / 树 /
 * 建库 / 上传 / 重建 / 删文档 / Key 与配置全部走控制面会话门；只有「拿真实 Key 打真实
 * 检索」这一件事必须走 Bearer。如果以后有人想在这个文件之外再加一个数据面调用，
 * 先回到 issue #92 的验收：「数据面客户端只剩一个调用点」。
 *
 * 请求体**只有** `kb_ids` + `query`：检索方式、条数、重排、改写全部来自这把 Key 的
 * 密钥配置（ADR-0010「第三次修订」），页面不拼任何质量字段。后端 `extra=forbid`，
 * 多带一个就会被 20002 拒掉——所以这里显式逐个字段组包，而不是把页面状态对象整体透传：
 * 以后谁在入参上多加一个字段，也不会顺着引用悄悄进请求体。
 */
export function retrieve(payload: RetrieveRequest): Promise<RetrieveData> {
  return dataHttp.post<RetrieveData>('/api/v1/rag/retrieve', {
    kb_ids: payload.kb_ids,
    query: payload.query,
  })
}
