/** 配置编辑器里那三条「看着像界面、其实是规则」的判定，集中成纯函数。 */
import {
  CUSTOM_TOP_K_DEFAULT,
  KEY_PROFILES,
  type CustomOptions,
  type KeyPolicyData,
  type KeyPolicyInput,
  type KeyPolicyPatch,
  type PolicyForm,
} from '../types/key'

/** `custom_options` 的四个合法键；顺序固定，补丁里的键顺序因此可断言。 */
const CUSTOM_OPTION_KEYS = ['mode', 'top_k', 'rerank', 'rewrite'] as const

/**
 * `custom` 的手工参数面板是否该出现。
 *
 * 只有当前 `profile` 是 `custom` 才为真：命名档位是**完整替换**，这批键在别的档位下
 * 一个都不读（后端 `profile_policy` 的契约）。界面据此用 `v-if` 而不是 `v-show`——
 * 切走必须**卸载**输入框，否则表单里会继续保留一批准则上已经失效的输入，下一次
 * 保存时 `buildPolicyPatch` 会把它们重新发出去。
 */
export function visibleCustomOptions(profile: string): boolean {
  return profile === 'custom'
}

/**
 * 套餐白名单之外的档位（要置灰并给原因的那几个）。
 *
 * 返回的是**已知四档里**不在 `allowedProfiles` 的那些：档位下拉照旧列全四档，
 * 被挡住的置灰 + hover 说明「当前套餐不支持该档位」。不返回「白名单里但前端不认识的
 * 档位名」——那不能变成一个可选项，只会让点下去之后被后端 20015 拒掉。
 */
export function disabledProfiles(allowedProfiles: readonly string[]): string[] {
  const allowed = new Set(allowedProfiles)
  return KEY_PROFILES.filter((profile) => !allowed.has(profile))
}

/**
 * 线上配置 → 编辑器表单。
 *
 * `custom_options` 缺键时补成「当前部署缺省」的可见值（vector / 5 / false / false）：
 * 编辑器里看到的应当就是「这次保存会写成什么」，而不是四个空白输入框加一句「留空等于
 * 跟部署走」——后者会让「用户没动它」与「配置里本来没有它」在表单上分不清。
 *
 * 数组是**拷贝**：编辑器改白名单时不能顺手改掉 vue-query 缓存里那一行配置。
 */
export function policyFormFrom(policy: KeyPolicyData): PolicyForm {
  return {
    allowed_kb_ids: [...policy.allowed_kb_ids],
    research_allowed: policy.research_allowed,
    hybrid_allowed: policy.hybrid_allowed,
    rerank_allowed: policy.rerank_allowed,
    query_rewrite_allowed: policy.query_rewrite_allowed,
    retrieve_qps: policy.retrieve_qps,
    retrieve_daily: policy.retrieve_daily,
    profile: policy.profile,
    include_parent: policy.include_parent,
    expand_table_rows: policy.expand_table_rows,
    custom_options: {
      mode: policy.custom_options.mode ?? 'vector',
      top_k: policy.custom_options.top_k ?? CUSTOM_TOP_K_DEFAULT,
      rerank: policy.custom_options.rerank ?? false,
      rewrite: policy.custom_options.rewrite ?? false,
    },
  }
}

/**
 * 表单相对原配置的**部分更新**补丁：只发真的变了的字段。
 *
 * 为什么不能整份 PATCH：`KeyPolicyPatchRequest` 是 `extra=forbid` + 部分更新语义，
 * 整份发出去等于把「用户没动过的字段」也重写一遍；一旦某次表单初始值与线上值有半点
 * 差异（例如补了 custom 默认值），保存就会悄悄改掉别的东西。
 *
 * 三条具体的判定：
 * - 白名单按**集合**比较：只是顺序变了不算改动；
 * - `custom_options` 只在当前档位是 `custom` 时才比较、且**逐键**发（后端是合并而不是
 *   整份替换）；切走时整批不发——命名档位不读它们；
 * - 其余字段逐个严格比较，布尔与数字都用 `!==`（`false` 与 `0` 是真实的值，不能被
 *   `||` 之类的写法吞掉）。
 */
export function buildPolicyPatch(form: PolicyForm, original: KeyPolicyData): KeyPolicyPatch {
  const patch: KeyPolicyPatch = {}

  if (!sameStringSet(form.allowed_kb_ids, original.allowed_kb_ids)) {
    patch.allowed_kb_ids = dedupeStrings(form.allowed_kb_ids)
  }
  if (form.research_allowed !== original.research_allowed) {
    patch.research_allowed = form.research_allowed
  }
  if (form.hybrid_allowed !== original.hybrid_allowed) {
    patch.hybrid_allowed = form.hybrid_allowed
  }
  if (form.rerank_allowed !== original.rerank_allowed) {
    patch.rerank_allowed = form.rerank_allowed
  }
  if (form.query_rewrite_allowed !== original.query_rewrite_allowed) {
    patch.query_rewrite_allowed = form.query_rewrite_allowed
  }
  if (form.retrieve_qps !== original.retrieve_qps) {
    patch.retrieve_qps = form.retrieve_qps
  }
  if (form.retrieve_daily !== original.retrieve_daily) {
    patch.retrieve_daily = form.retrieve_daily
  }
  if (form.profile !== original.profile) {
    patch.profile = form.profile
  }
  if (form.include_parent !== original.include_parent) {
    patch.include_parent = form.include_parent
  }
  if (form.expand_table_rows !== original.expand_table_rows) {
    patch.expand_table_rows = form.expand_table_rows
  }

  if (visibleCustomOptions(form.profile)) {
    const custom = changedCustomOptions(form.custom_options, original.custom_options)
    if (Object.keys(custom).length > 0) patch.custom_options = custom
  }

  return patch
}

/**
 * 签发时交给后端的**全量** 11 个值。
 *
 * 命名档位下 `custom_options` 发空对象而不是表单里的残留值：后端对命名档位是完整替换，
 * 这批键不生效，发过去只会让审计里出现一批「看起来配置了、实际没读」的参数。
 */
export function toPolicyInput(form: PolicyForm): KeyPolicyInput {
  return {
    allowed_kb_ids: dedupeStrings(form.allowed_kb_ids),
    research_allowed: form.research_allowed,
    hybrid_allowed: form.hybrid_allowed,
    rerank_allowed: form.rerank_allowed,
    query_rewrite_allowed: form.query_rewrite_allowed,
    retrieve_qps: form.retrieve_qps,
    retrieve_daily: form.retrieve_daily,
    profile: form.profile,
    include_parent: form.include_parent,
    expand_table_rows: form.expand_table_rows,
    custom_options: visibleCustomOptions(form.profile)
      ? {
          mode: form.custom_options.mode,
          top_k: form.custom_options.top_k,
          rerank: form.custom_options.rerank,
          rewrite: form.custom_options.rewrite,
        }
      : {},
  }
}

/** 只挑出相对原值真的变了的四个手工参数键。 */
function changedCustomOptions(
  form: PolicyForm['custom_options'],
  original: CustomOptions,
): CustomOptions {
  const changed: CustomOptions = {}
  for (const key of CUSTOM_OPTION_KEYS) {
    if (form[key] !== original[key]) {
      // 逐个键赋同一类型的值：四个键类型不同，联合赋值需要在这里收窄一次。
      Object.assign(changed, { [key]: form[key] })
    }
  }
  return changed
}

/** 去重并去掉空串（白名单里放一个空 ID 会被后端当参数错误拒掉）。 */
function dedupeStrings(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter((value) => value !== ''))]
}

/** 集合语义的相等：白名单只是顺序不同时不产生一次无意义的写入。 */
function sameStringSet(left: readonly string[], right: readonly string[]): boolean {
  const leftSet = new Set(dedupeStrings(left))
  const rightSet = new Set(dedupeStrings(right))
  if (leftSet.size !== rightSet.size) return false
  return [...leftSet].every((value) => rightSet.has(value))
}
