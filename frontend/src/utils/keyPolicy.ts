/** 配置编辑器里那几条「看着像界面、其实是规则」的判定，集中成纯函数。 */
import {
  POLICY_CAPABILITY_FIELDS,
  POLICY_PARAM_FIELDS,
  POLICY_QUOTA_FIELDS,
  type KeyPolicyData,
  type KeyPolicyInput,
  type KeyPolicyPatch,
  type PolicyForm,
} from '../types/key'

/** 除白名单外的全部 16 个具名字段；表单拷贝与补丁比较共用这一份清单，不会漏字段。 */
const SCALAR_FIELDS = [...POLICY_CAPABILITY_FIELDS, ...POLICY_QUOTA_FIELDS, ...POLICY_PARAM_FIELDS]

/**
 * 线上配置 → 编辑器表单。
 *
 * 只取 `KeyPolicyInput` 的字段：`policy_id` / `name` 不进表单——它们若混进去，
 * 签发时的 `toPolicyInput` 会把它们一起发出去，被后端 `extra=forbid` 判 20002。
 * 数组是**拷贝**：编辑器改白名单时不能顺手改掉 vue-query 缓存里那一行配置。
 */
export function policyFormFrom(policy: KeyPolicyData): PolicyForm {
  const form = {} as PolicyForm
  for (const key of SCALAR_FIELDS) {
    Object.assign(form, { [key]: policy[key] })
  }
  form.allowed_kb_ids = [...policy.allowed_kb_ids]
  return form
}

/**
 * 表单相对原配置的**部分更新**补丁：只发真的变了的字段。
 *
 * 为什么不能整份 PATCH：`KeyPolicyPatchRequest` 是 `extra=forbid` + 部分更新语义，
 * 整份发出去等于把「用户没动过的字段」也重写一遍；一旦某次表单初始值与线上值有半点
 * 差异，保存就会悄悄改掉别的东西。
 *
 * 两条具体的判定：
 * - 白名单按**集合**比较：只是顺序变了不算改动；
 * - 其余字段逐个严格比较，布尔与数字都用 `!==`（`false` 与 `0` 是真实的值，不能被
 *   `||` 之类的写法吞掉）。
 */
export function buildPolicyPatch(form: PolicyForm, original: KeyPolicyData): KeyPolicyPatch {
  const patch: KeyPolicyPatch = {}

  if (!sameStringSet(form.allowed_kb_ids, original.allowed_kb_ids)) {
    patch.allowed_kb_ids = dedupeStrings(form.allowed_kb_ids)
  }

  for (const key of SCALAR_FIELDS) {
    if (form[key] !== original[key]) {
      // 逐个键赋同一类型的值：各键类型不同，联合赋值需要在这里收窄一次。
      Object.assign(patch, { [key]: form[key] })
    }
  }

  return patch
}

/** 签发时交给后端的**全量**值：白名单去重去空串，其余照抄。 */
export function toPolicyInput(form: PolicyForm): KeyPolicyInput {
  return {
    ...form,
    allowed_kb_ids: dedupeStrings(form.allowed_kb_ids),
  }
}

/**
 * 参数预设按钮：把三项直参数一键填好，返回**新**表单对象。
 *
 * 预设只填这三项，不碰授权位与额度——重排预设打开的是「参数想要重排」，
 * 而「这把 Key 允许重排」是另一个授权位，得由用户自己在能力开关里打开。
 */
export function applyParamPreset(
  form: PolicyForm,
  preset: { mode: string; top_k: number; rerank: boolean },
): PolicyForm {
  return {
    ...form,
    retrieval_mode: preset.mode,
    retrieval_top_k: preset.top_k,
    retrieval_rerank: preset.rerank,
  }
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