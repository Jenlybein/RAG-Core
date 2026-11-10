/**
 * 模型配置页（`/admin/models`）的请求与响应形状。
 *
 * 逐字段抄自 `app/schemas/console_models.py`。两处刻意的写法：
 *
 * - **`api_key` 在列表与表单里永远是 masked**（`sk-…abcd`）。明文只有「显示原文」
 *   弹窗这一条出口，见 `ModelConfigSecretData`；
 * - **`roles` 由服务端算好**（`fast` / `strong`）。页面不再自己去 `GET /console/settings`
 *   拼「指针值 = 配置 id」这条知识——那等于让前端复述一遍后端已经算过的关系。
 */

/** 模型角色。键名 `model.role.<role>` 由后端 `app/core/model_roles.py` 推导。 */
export type ModelRole = 'fast' | 'strong'

/** 列表里的一行（也是新增 / 改配置后的那一行）。 */
export interface ModelConfigData {
  id: string
  label: string
  provider: string
  /** 空串表示用该供应商的默认地址。 */
  base_url: string
  model: string
  /** **永远 masked**。明文只从 `/{id}/secret` 出一次。 */
  api_key: string
  /** 现在指向这条配置的角色；没有角色指向时为空列表。 */
  roles: string[]
  created_at: string
  updated_at: string
}

/** `GET /console/model-configs` 的 data。 */
export interface ModelConfigListData {
  items: ModelConfigData[]
}

/** `POST /console/model-configs` 的请求体。`base_url` / `api_key` 空串合法。 */
export interface ModelConfigCreateRequest {
  label: string
  provider: string
  base_url: string
  model: string
  api_key: string
}

/**
 * `PATCH /console/model-configs/{id}` 的请求体：没发的字段不动。
 *
 * 编辑表单里 `api_key` 留空表示「不改」，所以本页只在用户真的填了时才发这个键；
 * 后端用 `exclude_unset` 表达同一语义（空串是「清掉 Key」，与「没发」不同）。
 */
export interface ModelConfigPatchRequest {
  label?: string
  provider?: string
  base_url?: string
  model?: string
  api_key?: string
}

/** `PATCH /console/model-configs/{id}` 的 data：改前 / 改后快照（已打码）。 */
export interface ModelConfigPatchData {
  id: string
  before: Record<string, unknown>
  after: Record<string, unknown>
  changed: string[]
}

/** `DELETE /console/model-configs/{id}` 的 data。 */
export interface ModelConfigDeleteData {
  id: string
  deleted: boolean
}

/** `GET /console/model-configs/{id}/secret` 的 data：明文只在这里出现一次。 */
export interface ModelConfigSecretData {
  id: string
  api_key: string
}
