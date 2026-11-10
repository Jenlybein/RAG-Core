import { consoleHttp } from '../lib/consoleHttp'
import type {
  DocumentDeleteReceipt,
  DocumentListItem,
  DocumentReindexReceipt,
  DocumentUploadReceipt,
} from '../types/document'
import type {
  ConsoleTreeData,
  ConsoleTreeDocument,
  CreatedKnowledgeBase,
  CreateKnowledgeBaseRequest,
  KnowledgeBaseDeleteReceipt,
  KnowledgeBaseNode,
} from '../types/knowledgeBase'

/**
 * 控制面「知识库与文档」接口（`app/api/routes/console_kbs.py`）。
 *
 * 本票 (#89) 先把**读**（树）切到控制面；#92 再把**写**（建库、上传、删文档、重建）
 * 切过来，于是上传页与列表页都不再打数据面。为什么单独放一个 `consoleKb.ts` 而不是
 * 塞进 `knowledgeBaseService.ts`：后者历史上是数据面（Bearer Key）的入口，本文件是
 * 唯一走控制面（会话 cookie）的知识库文件。把「哪扇门」提到文件粒度后，
 * `grep consoleHttp` 就能看全控制面接口。`fetchTree` 留在 `knowledgeBaseService`：
 * 它的签名被页面与轮询依赖，换个文件等于改调用点。
 *
 * 为什么页面一行不用改：端点直出的形状（`kb_id/name/split_strategy/allowed_formats/
 * chunk_size/documents[]`）与页面认的 `KnowledgeBaseNode[]` 一致；建库与上传/重建/删除的
 * 回执也复用数据面的模型（换的是门，不是契约）。
 */
export const CONSOLE_KB_TREE_PATH = '/console/knowledge-bases/tree'

/** 读树：租户来自会话，后端不做白名单收窄（控制面要看整个租户）。 */
export function fetchConsoleTree(): Promise<ConsoleTreeData> {
  return consoleHttp.get<ConsoleTreeData>(CONSOLE_KB_TREE_PATH)
}

/**
 * 建知识库（面板身份，含套餐静态配额）。
 *
 * 与数据面同一条规则：路径是 `POST /console/knowledge-bases`，请求体是 `CreateKBRequest`
 * （`extra=forbid`，多传一个字段就是 20002）。
 */
export function createKbConsole(
  payload: CreateKnowledgeBaseRequest,
): Promise<CreatedKnowledgeBase> {
  return consoleHttp.post<CreatedKnowledgeBase>('/console/knowledge-bases', payload)
}

/** 上传入口的两种形态：文件（multipart）与正文（JSON）。`kb_id` 恒在路径里。 */
export type UploadConsolePayload =
  | { kb_id: string; title: string; content: string }
  | { kb_id: string; title: string; file: File }

/**
 * 上传一份文档并异步索引（单端点按内容类型分流）。
 *
 * 为什么两个入口合成一个函数而不是各写一个：控制面路由本来就是**同一个 URL**，
 * 只是按 `content-type` 分流；前端也应当只有一个「上传」动作。文件形态刻意不手写
 * `Content-Type`：一旦写成 `multipart/form-data` 而没有 boundary，FastAPI 会解不出
 * `file` 字段并回 20002，这个坑只在真机上传时才会出现。
 */
export function uploadConsole(payload: UploadConsolePayload): Promise<DocumentUploadReceipt> {
  const url = `/console/knowledge-bases/${payload.kb_id}/documents`
  if ('file' in payload) {
    const form = new FormData()
    form.append('title', payload.title)
    form.append('file', payload.file)
    return consoleHttp.post<DocumentUploadReceipt>(url, form)
  }
  return consoleHttp.post<DocumentUploadReceipt>(url, {
    title: payload.title,
    content: payload.content,
  })
}

/**
 * 删除单文档：后端会清切片、删原文件、删记录，三步都不回滚。
 * 页面必须先二次确认再调它；服务层不做「可撤销」的承诺。
 */
export function deleteDocumentConsole(documentId: string): Promise<DocumentDeleteReceipt> {
  return consoleHttp.delete<DocumentDeleteReceipt>(`/console/documents/${documentId}`)
}

/**
 * 删除空知识库。
 *
 * **只对空库有效**：库内还有文档时后端回 `20015`（提示先删文档），而不是级联删除。
 * 级联会跨越「清向量 / 清关键词 / 删文件 / 删记录」四件事，中途失败就留下一半一半的
 * 状态——所以这条拒绝是有意的，页面要把 20015 的原文展示出来，不要翻译成「删除失败」。
 */
export function deleteKbConsole(kbId: string): Promise<KnowledgeBaseDeleteReceipt> {
  return consoleHttp.delete<KnowledgeBaseDeleteReceipt>(`/console/knowledge-bases/${kbId}`)
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
export function reindexDocumentConsole(
  documentId: string,
  options: ReindexDocumentOptions = {},
): Promise<DocumentReindexReceipt> {
  return consoleHttp.post<DocumentReindexReceipt>(
    `/console/documents/${documentId}/reindex`,
    undefined,
    { params: { reparse: options.reparse ?? false, force: options.force ?? false } },
  )
}

/**
 * 线上树 → 既有 `KnowledgeBaseNode[]`。
 *
 * 抽成纯函数而不是留在 `fetchTree` 里：映射是本次切换唯一真正变了的行为，
 * 纯函数让空库、多个库、字段缺省三种形状都能在 vitest 里直接钉住，不用起后端。
 *
 * 两个数组在这里兜底（`?? []`）：页面直接读 `documents.length`、遍历
 * `allowed_formats`，缺一个就是白屏；后端契约里它们恒在，但这里是渲染前
 * 最后一个还能「给安全默认」的位置。
 */
export function toKnowledgeBaseNodes(data: ConsoleTreeData): KnowledgeBaseNode[] {
  return data.knowledge_bases.map((node) => ({
    kb_id: node.kb_id,
    name: node.name,
    split_strategy: node.split_strategy,
    allowed_formats: node.allowed_formats ?? [],
    chunk_size: node.chunk_size,
    documents: (node.documents ?? []).map(toDocumentListItem),
  }))
}

/** 文档项逐字段搬过来并补齐 `error_message`：`StatusBadge` 拿 null 才不显示失败原因。 */
function toDocumentListItem(document: ConsoleTreeDocument): DocumentListItem {
  return {
    document_id: document.document_id,
    title: document.title,
    status: document.status,
    chunk_count: document.chunk_count,
    error_message: document.error_message ?? null,
    source_type: document.source_type,
    created_at: document.created_at,
  }
}
