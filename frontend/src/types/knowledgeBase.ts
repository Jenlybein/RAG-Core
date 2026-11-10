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
