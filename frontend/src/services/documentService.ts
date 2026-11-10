import { dataHttp } from '../lib/dataHttp'
import type {
  DocumentDeleteReceipt,
  DocumentReindexReceipt,
  DocumentUploadReceipt,
  UploadDocumentFileRequest,
  UploadDocumentJsonRequest,
} from '../types/document'

/**
 * JSON 入口：正文直接进库，后端把它固定按 markdown 准入。
 * 同样是异步回执——返回 `status=3` 只表示已登记 + 已投递。
 */
export function uploadDocument(
  payload: UploadDocumentJsonRequest,
): Promise<DocumentUploadReceipt> {
  return dataHttp.post<DocumentUploadReceipt>('/api/v1/documents/upload', payload)
}

/**
 * 文件入口：multipart 表单，浏览器负责字节与 boundary。
 *
 * 刻意不手写 `Content-Type`：一旦写成 `multipart/form-data` 而没有 boundary，
 * FastAPI 会解不出 file 字段并回 20002，这个坑只在真机上传时才会出现。
 */
export function uploadDocumentFile(
  payload: UploadDocumentFileRequest,
): Promise<DocumentUploadReceipt> {
  const form = new FormData()
  form.append('kb_id', payload.kb_id)
  form.append('title', payload.title)
  form.append('file', payload.file)
  return dataHttp.post<DocumentUploadReceipt>('/api/v1/documents/upload', form)
}

/*
 * 这里不再有「按库列文档」的读函数：列表页的树查询已在 #89 整体切到控制面
 * `GET /console/knowledge-bases/tree`（见 `consoleKb.ts`），数据面这条例行请求
 * 已无调用点，留着只会给下一个人一个「好像还在用这套 N+1」的错觉。
 * 文档项的 `DocumentListItem` 类型仍留在 `types/document.ts`：控制面树用它。
 */

/**
 * 删除单文档：后端会清切片、删原文件、删记录，三步都不回滚。
 *
 * 页面必须先二次确认再调它；服务层不做「可撤销」的承诺。
 */
export function deleteDocument(documentId: string): Promise<DocumentDeleteReceipt> {
  return dataHttp.delete<DocumentDeleteReceipt>(`/api/v1/documents/${documentId}`)
}

export interface ReindexDocumentOptions {
  /** 重新解析原始文件（忽略库内保存的正文）；默认沿用库内正文。 */
  reparse?: boolean
  /**
   * 对**成功**文档重建必须显式 `force=true`，否则后端回 20027：
   * 成功文档已有可用索引，静默覆盖等于替用户丢掉一份还能用的结果。
   * 失败 / 未成功的文档不需要 force；列表页的「重试」就属于这一种。
   */
  force?: boolean
}

/**
 * 重建单文档索引。
 *
 * 回执 `status` 恒为 3：这里只负责排队 + 投递，purge 与写入在 Worker 的事务里完成。
 * 所以调用方成功后不能把行改成「成功」，要等轮询看到终态。
 */
export function reindexDocument(
  documentId: string,
  options: ReindexDocumentOptions = {},
): Promise<DocumentReindexReceipt> {
  return dataHttp.post<DocumentReindexReceipt>(`/api/v1/documents/${documentId}/reindex`, undefined, {
    params: {
      reparse: options.reparse ?? false,
      force: options.force ?? false,
    },
  })
}
