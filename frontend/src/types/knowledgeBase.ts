import type { DocumentListItem } from './document'

/**
 * 建库接口的请求 / 响应类型与契约常量。
 *
 * 字段照 `step8/00-facts.md` §4；数值边界照 `app/models/knowledge_base.py`
 * （MIN/MAX/DEFAULT_CHUNK_SIZE 是同一个来源，前端只是把它显式化成可校验的副本）。
 *
 * ## 为什么这里没有「构建 / 版本」那一层（第 17 步·最后一批）
 *
 * 索引产物已经下放到**篇级**（`documents.current_index_generation` / `chunks.generation`）：
 * 每一篇各有自己的第几代产物，「整个库当前是第几版」这件事不再存在。于是控制面读模型里
 * 那一层字段——`build_state`（这篇与「当前这一版构建」的关系）、`needs_rebuild`（库级
 * 「该重建了」的判断，靠库的生效版本算）、`has_active_version`（库有没有生效版本）、
 * 构建行的进度（`documents_planned` / `documents_done` / `remaining` / `status` /
 * `activated_at`）——全部退场，本文件不再声明它们。
 *
 * 留下来的库级读数只有三个（都是**与构建无关**的聚合）：`document_count` / `chunk_count`
 * / `stale_documents`。最后一个是这次改造的对外读数：按篇比设置指纹，「N 篇的设置与产物
 * 不一致，重建后生效」。
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

/**
 * 重建一个库的回执。
 *
 * 语义已经不是「起一个构建」而是**按篇投递**：后端不做整库构建账本了，也没有单次请求
 * 无上限地投整个库——超过上限时分批，回执里两个数告诉你「这批投了多少、库里一共有多少」，
 * 两者不等就是碰到了上限，再点一次继续。
 */
export interface RebuildKbReceipt {
  kb_id: string
  /** 本次真的投进队列的篇数（抢不到的篇不计）。 */
  documents_dispatched: number
  /** 库里可重建的总篇数；大于 `documents_dispatched` 说明超过单次上限，再点一次继续。 */
  documents_total: number
}

/**
 * 改设置的回执：新设置 + 这次按篇投递了多少篇。
 *
 * 以前这里带 `build_id` / `build_version`（那次改设置起的整库构建）；构建层退场后
 * 改设置的效果也是「按篇重切」，回执只说投递量，别再让界面念一个不存在的版本号。
 */
export interface UpdateKbSettingsReceipt {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  section_part_max_chars: number
  /** 本次真的投进队列的篇数。 */
  documents_dispatched: number
  /** 库里可重建的总篇数；大于 `documents_dispatched` 说明要再点一次继续。 */
  documents_total: number
}

/**
 * 确认弹窗读的那份事实（`GET .../settings/preview`）。
 *
 * 只剩「当前设置」与「这次要重切多少」——`live_params` / `live_version` / `needs_rebuild`
 * 那一组是「库级生效版本」的读数，随构建层一起退场；`build_running` 同理（按篇投递之后
 * 没有「整库构建正在跑」这种东西可等）。
 */
export interface KbSettingsPreview {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  section_part_max_chars: number
  /** 这次会重切多少篇（起跑量，不是进度）。 */
  documents_to_rebuild: number
  /** 这次会重写多少片（同上，让人知道代价的量纲）。 */
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
 * 字段清单来自 `step8/00-facts.md` §11 的接缝约定。**第 17 步 #227 之后它不再是列表页的
 * 主读路径**（列表改用 `fetchKbList` 摘要、详情改用 `fetchKbDocuments` 分页），但密钥页
 * （勾选这把 Key 能查哪些库）仍然要「库 + 文档」一次拿全，所以类型留着。
 *
 * 数据面的 `GET /knowledge-bases` 列表类型（`KnowledgeBaseSummary` / `KnowledgeBaseListData`）
 * 已随 #92 删除：那个端点前端不再调用，留着只会让人以为还有第二个取库入口。
 */
export interface KnowledgeBaseNode {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  /** 节段阈值（第 16 步 #207）：超过就在片边界把一节切成几段。改它必须重建索引。 */
  section_part_max_chars: number
  /**
   * 「N 篇待更新」（现场九·第三步）：按篇比设置指纹得出的篇数。0 = 全部最新。
   *
   * 这是**这次改造的对外读数**：构建层退场之后，「要不要重建」只剩这一个按篇可数的数
   * （库级那句「需重建」靠生效版本参数，随版本一起没了）。
   */
  stale_documents: number
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
 * 数组与 `error_message` 标成可选，是因为下面两个类型同时是**映射层的入参**：
 * 后端契约里它们恒在，但映射是页面渲染前最后一个能给安全默认的地方，
 * 老缓存或半截负载缺字段时宁可给 `[]`/`null`，也别让页面读到 `undefined`。
 */
export type ConsoleTreeDocument = Omit<DocumentListItem, 'error_message'> & {
  /** 后端恒在；只有失败文档有文案，其余是 `null`。 */
  error_message?: string | null
}

export type ConsoleTreeKnowledgeBase = Omit<
  KnowledgeBaseNode,
  'allowed_formats' | 'documents' | 'section_part_max_chars' | 'stale_documents'
> & {
  allowed_formats?: string[]
  documents?: ConsoleTreeDocument[]
  /** 第 16 步 #207：老缓存 / 半截负载可能没有这两项，映射层给安全默认。 */
  section_part_max_chars?: number
  /** 「N 篇待更新」：老缓存里没有时映射层按 0（= 都是最新的）。 */
  stale_documents?: number
}

export interface ConsoleTreeData {
  knowledge_bases: ConsoleTreeKnowledgeBase[]
}

/** 四态各有多少篇（卡片上的「3 篇失败」与顶部「有文档在跑」都读它）。 */
export interface DocumentStatusCounts {
  success: number
  failed: number
  processing: number
  queued: number
}

/**
 * **库摘要**：卡片网格与详情 header 用的那一份，**不含文档数组**。
 *
 * 为什么从 `KnowledgeBaseNode` 里拆出来：树接口一次把整个租户的所有文档读出来，
 * 一个 5000 篇的库就是 5000 行；而卡片只显示四个数字。拆开之后「看库」与「看库里的
 * 文档」是两个请求、两种刷新节奏——详情因此才可能有「打开即取」这个动作。
 */
export interface KbSummary {
  kb_id: string
  name: string
  split_strategy: string
  allowed_formats: string[]
  chunk_size: number
  section_part_max_chars: number
  document_count: number
  status_counts: DocumentStatusCounts
  /** 这个库**当前代 × 当前稿**的切片总数（与检索口径一致，不是留存版本之和）。 */
  chunk_count: number
  /**
   * 「N 篇待更新」（现场九·第三步）：按篇比设置指纹得出的篇数。0 = 全部最新。
   *
   * 构建层退场后这是**唯一**一句「该重建了」的库级提示：它按篇可数，所以能直接说
   * 差几篇；原来那句「库级需重建」靠生效版本的参数快照，随版本一起没了。
   */
  stale_documents: number
  token_usage: { used: number; limit: number } | null
}

/**
 * 详情表的一行。
 *
 * 以前这里还有第二根轴 `build_state`（「这一篇与**当前这一版构建**的关系」）。构建层
 * 退场后参照物没了：每一篇自己就有当前代，状态四态本身就是它的进度。所以只剩文档自己
 * 的那一根轴。
 */
export interface KbDocumentRow {
  document_id: string
  title: string
  source_type: string
  /** 四态：1 成功 / 2 失败 / 3 索引中 / 4 排队中。 */
  status: number
  chunk_count: number
  token_count: number
  error_message: string | null
  current_version: number
  /** 这份文档落库的时刻（上传时写的）；刷新策略拿它判「最近一次写入」。 */
  created_at: string | null
}

/** 库列表（卡片网格）：只有摘要 + 服务端时间戳。 */
export interface KbListPayload {
  items: KbSummary[]
  /** 界面显示「数据更新于 14:03:12」用。没有它，「自动刷新停了」与「卡住了」长得一样。 */
  generated_at: string
}

/** 详情页一次拿全：库摘要 + 一页文档。 */
export interface KbDocumentsPayload {
  kb: KbSummary
  documents: KbDocumentRow[]
  total: number
  page: number
  page_size: number
  generated_at: string
}

/** 详情页取文档时的筛选。 */
export interface KbDocumentsQuery {
  page?: number
  pageSize?: number
  status?: number
  keyword?: string
}
