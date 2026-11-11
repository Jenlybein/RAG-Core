/**
 * 检索参数页（`/admin/retrieval`）的响应形状。
 *
 * 逐字段抄自 `app/schemas/console_settings.py`：`groups` 的顺序、`title` 文案、每行的
 * `source` / `invalid` / `why` 都是后端契约的一部分。页面**不自己编理由、不自己排序**，
 * 因为「为什么在这一档」的真相只在 `SETTING_SPECS` 一处（#93），前端再写一份就会漂移。
 *
 * `tier` 故意用裸 `string` 而不是 `'locked' | 'restart' | 'hot'`：将来后端加一档时，
 * 页面应当把那组照常渲染出来，而不是因为一个前端字面量联合类型而整页编译失败。
 */

/** 值来源：`.env` 是缺省，库是覆盖。 */
export type SettingSource = 'env' | 'db'

/** 一行有效值：白名单里的标量，或角色指针（空串 / `null` 表示「不指向」）。 */
export type SettingValue = boolean | number | string | null

/** 面板上的一行。 */
export interface SettingRow {
  key: string
  tier: string
  /** 后端语义是「PATCH 会不会接受它」；locked 档恒为 false。 */
  editable: boolean
  /** 有效值：有可采纳的覆盖就是库里的值，否则是 `.env` 的缺省值。 */
  value: SettingValue
  source: SettingSource
  /** 库里那一行读时校验失败、已回退成 `.env` 值。页面据此标红。 */
  invalid: boolean
  /** 「为什么在这一档」的人话，原样展示。 */
  why: string
  /** 标红行的原因；正常行为 `null`。 */
  invalid_reason: string | null
}

/** 一档的全部行。`title` 由服务端写好。 */
export interface SettingTierGroup {
  tier: string
  title: string
  editable: boolean
  settings: SettingRow[]
}

/** `GET /console/settings` 的 data。 */
export interface SettingsListData {
  groups: SettingTierGroup[]
  /** 缓存有效期（秒）。「各进程 Ns 内生效」的唯一依据，不许前端写死 30。 */
  cache_ttl_seconds: number
}

/** 影响面里的一组对象：`count` 是全量计数，`sample` 只是最多 5 个名字。 */
export interface ImpactGroup {
  count: number
  sample: string[]
}

/** 一次能力开关改动的试算结果。 */
export interface SettingsImpact {
  quality_degraded: ImpactGroup
  keyword_channel_lost: ImpactGroup
}

/**
 * `PATCH /console/settings` 的请求体。
 *
 * `dry_run` **不在 body 里**，它是查询参数（默认 true）。服务层只发 `values`。
 */
export interface SettingsPatchRequest {
  values: Record<string, SettingValue>
}

/** `PATCH /console/settings` 的 data：试算与真写共用这个形状。 */
export interface SettingsPatchData {
  dry_run: boolean
  /** 真正写进库的键（`dry_run=true` 时为空）。 */
  updated: string[]
  /** 这次改动里热生效 / 需重启的键。 */
  hot: string[]
  restart: string[]
  before: Record<string, unknown>
  after: Record<string, unknown>
  /** 只有试算才有影响面；真写时为 `null`。 */
  impact: SettingsImpact | null
  /** 服务端写好的提示（含「各进程 30s 内生效」）。页面仍然用 `cache_ttl_seconds`。 */
  note: string
}
