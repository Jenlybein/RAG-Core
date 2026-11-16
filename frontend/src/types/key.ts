/**
 * 控制面 Key 与密钥配置的类型：字段逐个照 `app/schemas/console_keys.py`，
 * 一个都不发明。
 *
 * 为什么要在前端再抄一份契约：`GET /console/keys` 一行的形状（明文 + masked + 它引用的
 * 那套配置）是页面的全部输入；只写 `unknown` 或随手 `any` 会让「后端加了一列」和
 * 「后端改了一列的名字」两种事故都等到运行时才暴露。这里显式列出来，`vue-tsc` 就会
 * 在字段名对不上时先报错。
 *
 * 几个刻意写下的契约事实：
 *
 * - **`policy` 可以是 `null`**（结构上成立：配置独立于 Key，删 Key 不删配置，反过来一条
 *   指向已不存在配置的 Key 也可能存在）。页面不能拿它当「一定有配置」来渲染；
 * - **`key_value` 是明文**（ADR-0004）：列表接口本来就回原文，因为调试台要拿它打真实接口。
 *   masked 只是摘要形态；页面默认只显示 masked，看原文必须二次确认，且不落任何 storage；
 * - **第 13 步起没有档位**（ADR-0032）：`profile` / `custom_options` 已从后端表里删除，
 *   用多好由 `retrieval_mode` / `retrieval_top_k` / `retrieval_rerank` 三项直参数表达。
 *   界面上的 speed / balanced / quality 只是「一键填好这三项」的预设按钮，不是契约字段。
 */

/** 三项直参数里的检索方式；与 `app/core/config.py` 的 `SUPPORTED_RETRIEVAL_MODES` 同源。 */
export const RETRIEVAL_MODES = ['vector', 'bm25', 'hybrid'] as const
export type RetrievalMode = (typeof RETRIEVAL_MODES)[number]

/** `retrieval_top_k` 的平台上下限（`app/services/retrieval_options.py` 的 MIN/MAX_TOP_K）。 */
export const TOP_K_MIN = 1
export const TOP_K_MAX = 50
/** 新建配置时的默认返回条数：与后端 `KeyPolicyInput.retrieval_top_k` 的默认值一致。 */
export const TOP_K_DEFAULT = 5

/** 五项授权位对应的配置字段名；清单与后端 `KeyPolicyData` 的第二组一致。 */
export const POLICY_CAPABILITY_FIELDS = [
  'retrieve_allowed',
  'rewrite_allowed',
  'decompose_allowed',
  'research_allowed',
  'rerank_allowed',
] as const
export type PolicyCapabilityField = (typeof POLICY_CAPABILITY_FIELDS)[number]

/** 三本账 × 两格的六个额度字段；`retrieve_*` 必须 ≥1，其余四格 0 合法（= 不给额度）。 */
export const POLICY_QUOTA_FIELDS = [
  'retrieve_qps',
  'retrieve_daily',
  'rerank_qps',
  'rerank_daily',
  'llm_qps',
  'llm_daily',
] as const
export type PolicyQuotaField = (typeof POLICY_QUOTA_FIELDS)[number]

/** 三项直参数 + 两个形状开关。 */
export const POLICY_PARAM_FIELDS = [
  'retrieval_mode',
  'retrieval_top_k',
  'retrieval_rerank',
  'include_parent',
  'expand_table_rows',
] as const

/**
 * 参数预设：只是把三项直参数一键填好的按钮，**不是契约字段**。
 *
 * 后端不再有档位概念（ADR-0032）：点一下等于替用户填三个输入框，之后每一项都可以
 * 单独再改。预设不承诺链路、不参与判定，所以这里不导出「档位类型」。
 */
export const PARAM_PRESETS = [
  { name: 'speed', hint: '只走向量检索', mode: 'vector', top_k: TOP_K_DEFAULT, rerank: false },
  { name: 'balanced', hint: '向量 + 关键词', mode: 'hybrid', top_k: TOP_K_DEFAULT, rerank: false },
  { name: 'quality', hint: '混合检索 + 重排', mode: 'hybrid', top_k: TOP_K_DEFAULT, rerank: true },
] as const satisfies readonly {
  name: string
  hint: string
  mode: RetrievalMode
  top_k: number
  rerank: boolean
}[]

/**
 * 一套配置的**全量**五组值（`KeyPolicyData` 去掉 id 与 name）。
 *
 * 签发时与后端 `KeyPolicyInput` 的默认值同义：只开基线 `retrieve` 一个位、加工能力的
 * 两本账全 0、形状全开、白名单为空（空 = 什么都没圈定，不是「全部」）。
 * `retrieve_qps` / `retrieve_daily` 没有默认值——它们必须由人填。
 */
export interface KeyPolicyInput {
  /** 第一组：数据范围。空列表 = 什么都没圈定（不是「全部」）。 */
  allowed_kb_ids: string[]
  /** 第二组：五个授权位。 */
  retrieve_allowed: boolean
  rewrite_allowed: boolean
  decompose_allowed: boolean
  research_allowed: boolean
  rerank_allowed: boolean
  /** 第三组：三本账额度（Key 级那一半）。 */
  retrieve_qps: number
  retrieve_daily: number
  rerank_qps: number
  rerank_daily: number
  llm_qps: number
  llm_daily: number
  /** 第四组：三项直参数。 */
  retrieval_mode: string
  retrieval_top_k: number
  retrieval_rerank: boolean
  /** 第五组：结果形状。 */
  include_parent: boolean
  expand_table_rows: boolean
}

/** 一套配置（列表 / 签发 / 改配置的响应都用这一个形状）。 */
export interface KeyPolicyData extends KeyPolicyInput {
  policy_id: string
  name: string
}

/** 一条 Key（列表的一行，也是签发成功的返回值）。 */
export interface KeyData {
  key_id: string
  label: string | null
  /** 明文。只进内存：显示它必须二次确认，**绝不写 localStorage / sessionStorage / URL**。 */
  key_value: string
  /** `rc_live_…9f2c`，列表与日志用的第二形态。 */
  key_masked: string
  status: string
  /** 配置缺失（或指向别的租户的坏数据）时如实为 `null`，不是编一套默认值。 */
  policy: KeyPolicyData | null
}

export interface KeyListData {
  keys: KeyData[]
}

/** 签发请求：`label` 只是备注名，鉴权永远不读它。 */
export interface KeyIssueRequest {
  label?: string | null
  policy: KeyPolicyInput
}

/** 改配置的请求：**部分更新**，没发的字段（以及发 `null` 的）都表示「这次不动它」。 */
export type KeyPolicyPatch = Partial<KeyPolicyInput>

/** 改配置的回执：真变了的字段在 `changed` 里，空列表表示给出的字段都与原值相同。 */
export interface KeyPolicyPatchResult {
  policy_id: string
  before: Record<string, unknown>
  after: Record<string, unknown>
  changed: string[]
}

/** 删 Key 的回执（配置不跟着删）。 */
export interface KeyDeleteReceipt {
  key_id: string
  deleted: boolean
}

/**
 * 配置编辑器的表单状态。
 *
 * 第 13 步起与 `KeyPolicyInput` 完全同形：所有字段都是具名的具体值（不再有「缺键 =
 * 跟部署缺省走」的自由字典），`el-select` / `el-input-number` / `el-switch` 直接绑。
 */
export type PolicyForm = KeyPolicyInput

/** 可勾选的知识库选项（来自控制面树，只取页面要用的两列）。 */
export interface KbOption {
  kb_id: string
  name: string
}