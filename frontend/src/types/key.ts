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
 * - `custom_options` 是 `dict`。这里只声明已知的四个键（`mode` / `top_k` / `rerank` /
 *   `rewrite`），其余键不会有；把整个对象说成 `Record<string, any>` 等于放弃这层保护。
 */

/** 四个质量档位名；与后端 `app/models/key_policy.py` 的 `PROFILES` 同一套词。 */
export const KEY_PROFILES = ['speed', 'balanced', 'quality', 'custom'] as const
export type KeyProfile = (typeof KEY_PROFILES)[number]

/** 档位的中文说明，只用于界面展示，不参与任何判定。 */
export const KEY_PROFILE_LABELS: Record<KeyProfile, string> = {
  speed: 'speed（向量检索，快）',
  balanced: 'balanced（混合检索）',
  quality: 'quality（混合 + 重排 + 改写）',
  custom: 'custom（按下面四个手动参数）',
}

/** `custom` 档的手工检索方式；与 `app/core/config.py` 的 `SUPPORTED_RETRIEVAL_MODES` 同源。 */
export const CUSTOM_OPTION_MODES = ['vector', 'bm25', 'hybrid'] as const
export type CustomOptionMode = (typeof CUSTOM_OPTION_MODES)[number]

/** `custom` 档 `top_k` 的平台上下限（`app/services/retrieval_options.py` 的 MIN/MAX_TOP_K）。 */
export const CUSTOM_TOP_K_MIN = 1
export const CUSTOM_TOP_K_MAX = 50
/** 新建一行 custom 参数时的默认返回条数：与检索响应里缺省 `top_k` 的常用值一致。 */
export const CUSTOM_TOP_K_DEFAULT = 5

/**
 * `custom` 档才读的那批手工参数。
 *
 * 全部可选：**缺键意味着「跟部署缺省走」**，而不是「关掉」。命名档位下这批键不生效
 * （命名档位是完整替换，见 GLOSSARY「档位」）。
 */
export interface CustomOptions {
  mode?: string
  top_k?: number
  rerank?: boolean
  rewrite?: boolean
}

/**
 * 一套配置的**全量**五组值（`KeyPolicyData` 去掉 id 与 name）。
 *
 * 签发时 `KeyPolicyInput` 的默认值与后端 `key_policies` 列默认值同义；档位、QPS、日配额
 * 没有默认值——它们必须由人填，界面也不该替用户猜一个「看起来合理」的额度。
 */
export interface KeyPolicyInput {
  /** 第一组：数据范围。空列表 = 什么都没圈定（不是「全部」）。 */
  allowed_kb_ids: string[]
  /** 第二组：能力开关。 */
  research_allowed: boolean
  hybrid_allowed: boolean
  rerank_allowed: boolean
  query_rewrite_allowed: boolean
  /** 第三组：限额。 */
  retrieve_qps: number
  retrieve_daily: number
  /** 第四组：档位。 */
  profile: string
  /** 第五组：结果形状。 */
  include_parent: boolean
  expand_table_rows: boolean
  custom_options: CustomOptions
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
 * 与线上 `KeyPolicyData` 的差别只有 `custom_options`：表单里四个键**始终有具体值**，
 * 因为 `el-select` / `el-input-number` / `el-switch` 绑不到 `undefined`；
 * 「缺键 = 跟部署缺省走」的语义在 `policyFormFrom` 补默认值时已经还原成「编辑器里看到的
 * 就是将要保存的」，而 `buildPolicyPatch` 只把真的变了的键发出去。
 */
export interface PolicyForm extends Omit<KeyPolicyInput, 'custom_options'> {
  custom_options: {
    mode: string
    top_k: number
    rerank: boolean
    rewrite: boolean
  }
}

/** 可勾选的知识库选项（来自控制面树，只取页面要用的两列）。 */
export interface KbOption {
  kb_id: string
  name: string
}
