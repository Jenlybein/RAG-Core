/**
 * 管理员面板三页（账号 / 套餐 / 审计）的请求与响应形状。
 *
 * 逐字段抄自 `app/schemas/console.py`，不是照「REST 应该长什么样」猜的：这组接口的字段名
 * 与嵌套（`account.plan`、`impact.over_max_kb.count`、`audit_logs`）都是后端契约的一部分，
 * 猜错只会在运行时变成 undefined 而不会在编译期报错。
 *
 * 这里只取三页用得到的字段；后端多给的字段进不了前端，少给的字段在使用处被 TS 拦住。
 */

/** 套餐快照 + 主键与更新时间（`PlanData`，管理员视角）。 */
export interface PlanData {
  plan_id: number
  code: string
  name: string
  allowed_profiles: string[]
  hybrid_allowed: boolean
  rerank_allowed: boolean
  query_rewrite_allowed: boolean
  research_allowed: boolean
  retrieve_qps: number
  retrieve_daily: number
  max_kb: number
  max_kb_per_retrieve: number
  max_documents_per_kb: number
  max_processing_documents: number
  /** 后端是 `datetime`；没有时区后缀，前端按本地时间展示（见 `utils/datetime.ts`）。 */
  updated_at: string | null
}

/** `GET /console/plans` 的 data。 */
export interface PlanListData {
  plans: PlanData[]
}

/** `GET /console/accounts` 的一行：账号 + 它所属租户 + 那个租户当前的套餐。 */
export interface AccountItem {
  account_id: string
  username: string
  role: string
  status: string
  tenant_id: string | null
  tenant_name: string | null
  tenant_status: string | null
  /** 管理员不属于任何租户，这里为 `null`。 */
  plan: PlanData | null
}

/** `GET /console/accounts` 的 data。 */
export interface AccountListData {
  accounts: AccountItem[]
}

/**
 * `PATCH /console/accounts/{id}` 的请求体。
 *
 * 后端 `extra=forbid`，所以键名只允许这三个；`undefined` 的键会被 axios 从 JSON 里丢掉，
 * 与「不发这个键」是同一件事（后端用 `exclude_none` 表达「这次不动它」）。
 */
export interface AccountPatchRequest {
  /** 换的是账号所属**租户**的套餐：套餐挂在租户上。 */
  plan_id?: number
  /** `active` / `disabled`。 */
  status?: string
  /** `active` / `disabled`；管理员账号没有租户，发这个会被 20002 拒绝。 */
  tenant_status?: string
}

/** `PATCH /console/accounts/{id}` 的 data：改前 / 改后的三键快照。 */
export interface AccountPatchData {
  account_id: string
  before: Record<string, unknown>
  after: Record<string, unknown>
}

/** 影响面里的一组对象：`count` 是全量计数，`sample` 只是最多 5 个名字。 */
export interface ImpactGroup {
  count: number
  sample: string[]
}

/** 套餐改动的试算结果（`PlanImpactData`）。 */
export interface PlanImpact {
  tenants_checked: number
  over_max_kb: ImpactGroup
  lose_profile: ImpactGroup
  policies_losing_profile: number
}

/** 改 / 删套餐的请求体（`PlanPatchRequest`）；`null` 与「不发」等价，这里用可选键表达。 */
export interface PlanPatchBody {
  operation?: 'update' | 'delete'
  code?: string
  name?: string
  allowed_profiles?: string[]
  hybrid_allowed?: boolean
  rerank_allowed?: boolean
  query_rewrite_allowed?: boolean
  research_allowed?: boolean
  retrieve_qps?: number
  retrieve_daily?: number
  max_kb?: number
  max_kb_per_retrieve?: number
  max_documents_per_kb?: number
  max_processing_documents?: number
}

/** `PATCH /console/plans/{id}` 的 data：试算与真写共用这个形状。 */
export interface PlanPatchData {
  plan_id: number
  dry_run: boolean
  deleted: boolean
  before: PlanData | null
  after: PlanData | null
  /** 只有试算（`dry_run=true`）的 update 才有；delete 与真写都是 `null`。 */
  impact: PlanImpact | null
}

/** `GET /console/audit-logs` 的一行。 */
export interface AuditLogItem {
  audit_id: string
  /** 系统动作没有操作者，如实为 `null`（页面显示「系统」而不是编一个账号）。 */
  actor_id: string | null
  action: string
  target: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  created_at: string
}

/** `GET /console/audit-logs` 的 data。 */
export interface AuditLogListData {
  audit_logs: AuditLogItem[]
}

/** 六个「≥ 1」的数值上限；清单来自后端 `PLAN_NUMERIC_COLUMNS`。 */
export type PlanNumericField =
  | 'retrieve_qps'
  | 'retrieve_daily'
  | 'max_kb'
  | 'max_kb_per_retrieve'
  | 'max_documents_per_kb'
  | 'max_processing_documents'

/** 四个能力开关。 */
export type PlanCapabilityField =
  | 'hybrid_allowed'
  | 'rerank_allowed'
  | 'query_rewrite_allowed'
  | 'research_allowed'

/**
 * 套餐编辑表单真正能改的字段。
 *
 * `plan_id` 与 `code` 不在里面：`code` 是脚本与日志指代这一档的名字（后端另有唯一约束），
 * 在一个勾选框面板里顺手改名，代价是所有既有脚本与文档的指代悄悄失效。名字 `name` 才是
 * 给人看的展示值，所以只有它可编辑。
 */
export type PlanEditableFields = Pick<
  PlanData,
  | 'name'
  | 'allowed_profiles'
  | 'hybrid_allowed'
  | 'rerank_allowed'
  | 'query_rewrite_allowed'
  | 'research_allowed'
  | 'retrieve_qps'
  | 'retrieve_daily'
  | 'max_kb'
  | 'max_kb_per_retrieve'
  | 'max_documents_per_kb'
  | 'max_processing_documents'
>
