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

/**
 * 文档列表行（`GET /documents?kb_id=`）。
 *
 * 列表页把它挂在知识库父节点下当子行；字段照 `step8/00-facts.md` §4，
 * 不在这里加 `tenant_id` 之类的后端内部字段——列表页用不到，加了只会诱使页面越权。
 */
export interface DocumentListItem {
  document_id: string
  title: string
  /** 1 成功 / 2 失败 / 3 索引中；映射只走 `StatusBadge`。 */
  status: number
  chunk_count: number
  error_message: string | null
  source_type: string
  created_at: string
}

/** 文档列表响应体：服务层在这里剥掉外层 `items`，页面只拿到数组。 */
export interface DocumentListData {
  items: DocumentListItem[]
}

/**
 * 重建回执。`status` 恒为 3：路由只做「排队 + 投递」，purge 与写入仍归 Worker，
 * 因此重建成功后也要等轮询看到终态，不能就地改成成功。
 */
export interface DocumentReindexReceipt {
  document_id: string
  status: number
  chunk_count: number
  reparse: boolean
}

/** 删除回执。`deleted=true` 是后端确认，不是前端乐观更新。 */
export interface DocumentDeleteReceipt {
  document_id: string
  deleted: boolean
}
