import type { DocumentListItem } from './document'

/**
 * 建库接口的请求 / 响应类型与契约常量。
 *
 * 字段照 `step8/00-facts.md` §4；数值边界照 `app/models/knowledge_base.py`
 * （MIN/MAX/DEFAULT_CHUNK_SIZE 是同一个来源，前端只是把它显式化成可校验的副本）。
 */

/** 切块策略。**必填无默认**：它同时决定这个库收哪些格式，替用户选等于替他决定语料类型。 */
export const KB_SPLIT_STRATEGIES = ['structure', 'size'] as const
export type KbSplitStrategy = (typeof KB_SPLIT_STRATEGIES)[number]

/** 后端 FormatKind 的完整清单；structure 策略只接受其中的 markdown。 */
export const KB_FORMATS = ['markdown', 'text', 'docx', 'pdf', 'html'] as const
export type KbFormat = (typeof KB_FORMATS)[number]

/** 切块窗口范围与默认值（单位 token，Qwen 分词口径）。 */
export const KB_CHUNK_SIZE_MIN = 200
export const KB_CHUNK_SIZE_MAX = 2000
export const KB_CHUNK_SIZE_DEFAULT = 512

/** `POST /knowledge-bases/create` 的请求体；后端 `extra=forbid`，不能多传字段。 */
export interface CreateKnowledgeBaseRequest {
  name: string
  description?: string
  split_strategy: KbSplitStrategy
  allowed_formats?: KbFormat[]
  chunk_size?: number
}

/** 建库回执：`kb_id` 就是要回填到上传表单的那个值。 */
export interface CreatedKnowledgeBase {
  kb_id: string
  name: string
  tenant_id: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  created_at: string
}

/**
 * `GET /knowledge-bases` 的列表项。
 *
 * 与建库回执相比少了 `created_at`，多了 `tenant_id`；列表页不展示这两项，
 * 但服务层仍需按后端原样声明，免得解析阶段丢掉真实存在的字段。
 */
export interface KnowledgeBaseSummary {
  kb_id: string
  name: string
  tenant_id: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
}

/** `GET /knowledge-bases` 的响应体外壳。 */
export interface KnowledgeBaseListData {
  knowledge_bases: KnowledgeBaseSummary[]
}

/**
 * 列表页的两级树节点：父是知识库，`documents` 是它的子行。
 *
 * 字段清单来自 `step8/00-facts.md` §11 的接缝约定：阶段 1 由客户端拼装，
 * #89 的 `/console/knowledge-bases/tree` 会直出同一形状。页面与轮询只认这个类型，
 * 所以换实现时它们不需要改。
 */
export interface KnowledgeBaseNode {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  documents: DocumentListItem[]
}

/**
 * 控制面树接口（`GET /console/knowledge-bases/tree`）的响应 data。
 *
 * 与 `KnowledgeBaseNode` 是同一形状——这正是 #89 只换实现、不动页面的前提
 * （`app/schemas/console_kbs.py` 的 `KnowledgeBaseTreeData` 照着前端类型定的）。
 *
 * 数组与 `error_message` 标成可选，是因为下面三个类型同时是**映射层的入参**：
 * 后端契约里它们恒在，但映射是页面渲染前最后一个能给安全默认的地方，
 * 老缓存或半截负载缺字段时宁可给 `[]`/`null`，也别让页面读到 `undefined`。
 */
export type ConsoleTreeDocument = Omit<DocumentListItem, 'error_message'> & {
  /** 后端恒在；只有失败文档有文案，其余是 `null`。 */
  error_message?: string | null
}

export type ConsoleTreeKnowledgeBase = Omit<KnowledgeBaseNode, 'allowed_formats' | 'documents'> & {
  allowed_formats?: string[]
  documents?: ConsoleTreeDocument[]
}

export interface ConsoleTreeData {
  knowledge_bases: ConsoleTreeKnowledgeBase[]
}
