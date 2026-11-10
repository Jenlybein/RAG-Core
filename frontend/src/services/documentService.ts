import { dataHttp } from '../lib/dataHttp'
import type {
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
