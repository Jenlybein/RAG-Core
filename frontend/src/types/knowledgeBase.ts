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

/** 节段阈值范围与默认值（单位字符，第 16 步 #207）。与后端 `knowledge_base.py` 同源。 */
export const KB_SECTION_PART_MIN = 500
export const KB_SECTION_PART_MAX = 10000
export const KB_SECTION_PART_DEFAULT = 2000

/** `POST /knowledge-bases/create` 的请求体；后端 `extra=forbid`，不能多传字段。 */
export interface CreateKnowledgeBaseRequest {
  name: string
  description?: string
  split_strategy: KbSplitStrategy
  allowed_formats?: KbFormat[]
  chunk_size?: number
  /** 节段阈值：可选，缺省 2000。 */
  section_part_max_chars?: number
}

/** 建库回执：`kb_id` 就是要回填到上传表单的那个值。 */
export interface CreatedKnowledgeBase {
  kb_id: string
  name: string
  tenant_id: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  section_part_max_chars: number
  created_at: string
}

/** `PATCH /console/knowledge-bases/{kb_id}/settings` 的请求体。 */
export interface UpdateKbSettingsRequest {
  name?: string
  description?: string
  split_strategy?: KbSplitStrategy
  chunk_size?: number
  section_part_max_chars?: number
  /** 显式二次确认：这次改动会触发整库重建。后端缺它即 20002。 */
  confirm: boolean
}

/** 改设置的回执：新设置 + 这次重建的版本。 */
/** 重建一个库的回执（设置没变，所以不回吐设置）。 */
export interface RebuildKbReceipt {
  kb_id: string
  build_id: string
  build_version: string
  /** 这次要重切多少篇（起跑快照）。 */
  documents_planned: number
}

export interface UpdateKbSettingsReceipt {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  section_part_max_chars: number
  build_id: string
  build_version: string
}

/** 确认弹窗读的那份事实（`GET .../settings/preview`）。 */
export interface KbSettingsPreview {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  section_part_max_chars: number
  /** 生效那一版的构建参数；`null` = 老版本没留参数。 */
  live_params: Record<string, unknown> | null
  live_version: string | null
  needs_rebuild: boolean
  build_running: boolean
  documents_to_rebuild: number
  chunks_to_rebuild: number
}

/** `DELETE /console/knowledge-bases/{kb_id}` 的回执；只对空库成功（库内有文档是 20015）。 */
export interface KnowledgeBaseDeleteReceipt {
  kb_id: string
  deleted: boolean
}

/**
 * 列表页的两级树节点：父是知识库，`documents` 是它的子行。
 *
 * 字段清单来自 `step8/00-facts.md` §11 的接缝约定：阶段 1 由客户端拼装，
 * #89 的 `/console/knowledge-bases/tree` 会直出同一形状。页面与轮询只认这个类型，
 * 所以换实现时它们不需要改。
 *
 * 数据面的 `GET /knowledge-bases` 列表类型（`KnowledgeBaseSummary` / `KnowledgeBaseListData`）
 * 已随 #92 删除：那个端点前端不再调用，留着只会让人以为还有第二个取库入口。
 */
/** 「未入新版本」的一条明细（#216）：哪一篇、叫什么、为什么。 */
export interface BuildFailure {
  document_id: string
  title: string
  reason: string
}

/**
 * 进行中的整库构建（#216）。**没有构建时后端回 `null`，不是空对象**——
 * 页面按 `null` 才不显示进度条，空对象会让「进度 0/0」的假条长期挂在那里。
 */
export interface BuildProgress {
  build_id: string
  version: string
  /** 参与篇数快照（分母）。 */
  planned: number
  /** 已有结果的篇数（失败也算有结果）。 */
  done: number
  /** 还没有结果的篇数。 */
  pending: number
  /** 未入新版本的明细（后端最多回 5 条）。 */
  failures: BuildFailure[]
  /** 失败总数（`failures` 可能被截断，这个数不截断）。 */
  failure_count: number
}

export interface KnowledgeBaseNode {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  /** 节段阈值（第 16 步 #207）：超过就在片边界把一节切成几段。改它必须重建索引。 */
  section_part_max_chars: number
  /** 库当前设置与生效那一版的构建参数已不一致 → 这个库需要重建才生效。 */
  needs_rebuild: boolean
  /** 进行中的构建（#216）；`null` = 这个库没有构建在跑。 */
  build_progress: BuildProgress | null
  /**
   * 库级 token 容量占用（第 17 步 #221 / 用户故事 #13）；`null` = 容量读不到的降级路径。
   *
   * `used` 是**已落库**的 `token_count` 合计——与容量硬拦同一个数，所以进度条不会与
   * 「传上去被拒」自相矛盾。
   */
  token_usage?: { used: number; limit: number } | null
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

export type ConsoleTreeKnowledgeBase = Omit<
  KnowledgeBaseNode,
  'allowed_formats' | 'documents' | 'section_part_max_chars' | 'needs_rebuild'
> & {
  allowed_formats?: string[]
  documents?: ConsoleTreeDocument[]
  /** 第 16 步 #207：老缓存 / 半截负载可能没有这两项，映射层给安全默认。 */
  section_part_max_chars?: number
  needs_rebuild?: boolean
}

export interface ConsoleTreeData {
  knowledge_bases: ConsoleTreeKnowledgeBase[]
}
