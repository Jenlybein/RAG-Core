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

/*
 * 上传请求体的两种形态（JSON / multipart）随 `documentService.ts` 一起搬到了
 * `services/consoleKb.ts` 的 `UploadConsolePayload`：控制面把 `kb_id` 放进路径，
 * 请求体只收 `title` / `content` / `file`，与数据面那套入参不同，所以这里不再声明它们。
 */

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

/**
 * 一份文档的原文（`GET /console/documents/{id}/content`，控制面只读）。
 *
 * `truncated` 与 `length` 是一对：前者只说「给全了没有」，后者是**原文真实长度**。
 * 页面必须把截断说出来——只显示截断后的文本会让人以为文档就这么长。
 */
export interface DocumentContent {
  document_id: string
  title: string
  source_type: string
  source_filename: string | null
  /** 1 成功 / 2 失败 / 3 索引中；原文能看与索引成功是两件事。 */
  status: number
  content: string
  truncated: boolean
  length: number
}
