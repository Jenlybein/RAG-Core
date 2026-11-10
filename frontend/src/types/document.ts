/** 数据面文档接口的请求 / 回执类型。字段照 `step8/00-facts.md` §4，不自己发明。 */

/** 上传回执。异步化后上传只登记 + 投递，`status` 恒为 3（处理中）。 */
export interface DocumentUploadReceipt {
  document_id: string
  kb_id: string
  /** 1 成功 / 2 失败 / 3 处理中（`app/utils/status.py` 的 DocumentStatus）。 */
  status: number
  chunk_count: number
  error_message: string | null
  source_type: string
}

/** JSON 入口的请求体：正文直接落库，入口固定按 markdown 准入。 */
export interface UploadDocumentJsonRequest {
  kb_id: string
  title: string
  content: string
}

/** 文件入口的请求体：multipart 表单，`file` 由浏览器提供字节。 */
export interface UploadDocumentFileRequest {
  kb_id: string
  title: string
  file: File
}
