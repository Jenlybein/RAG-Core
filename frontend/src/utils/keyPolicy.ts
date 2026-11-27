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
import type { ConsolePlanSnapshot } from '../types/console'

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

/**
 * 吃「大模型」这本账的三个能力位。
 *
 * 为什么单列这三项而不管 `rerank_allowed`：重排额度用尽时正确行为是**跳过重排并如实标注**
 * （ADR-0032），所以「重排位开着、额度 0」是一个可用状态；而改写 / 分解 / Research 是
 * **显式调用**，没有「额度不够就跳过」这一档，额度 0 时每一次都会失败。
 */
export const LLM_CAPABILITY_FIELDS = [
  'rewrite_allowed',
  'decompose_allowed',
  'research_allowed',
] as const

type LlmCapabilityField = (typeof LLM_CAPABILITY_FIELDS)[number]

/** 有没有打开任何一个用大模型的能力位。 */
export function llmCapabilitiesOpen(
  form: Pick<KeyPolicyInput, LlmCapabilityField>,
): boolean {
  return LLM_CAPABILITY_FIELDS.some((field) => form[field])
}

/**
 * 「能力位开着、模型额度两格都是 0」—— 一份**注定失败**的配置。
 *
 * 现场长这样：用户在能力开关里勾上「查询改写」，高级设置里的「模型每秒/每日上限」还是默认
 * 的 0，于是每次「改写后检索」都被拒。而这个组合在界面上一句话都没说——用户只能在下一次
 * 调用时收到一句「操作太频繁」，那是个让他永远等不到头的错误。
 *
 * 返回真表示**需要**把额度补上（不是「已经合法」）。判据只看「两格是否都为 0」：只要有一格
 * 有额度，这本账就已经启用，另一种额度用尽的情形由运行时报 20014（等重置）。
 */
export function llmQuotaUnfunded(
  form: Pick<KeyPolicyInput, LlmCapabilityField | 'llm_qps' | 'llm_daily'>,
): boolean {
  return llmCapabilitiesOpen(form) && form.llm_qps <= 0 && form.llm_daily <= 0
}

/**
 * 把模型额度的 0 按**套餐上限**补上（在各格本身仍为 0 时）。
 *
 * 为什么是「补」而不是「拦」：勾上能力位的那一刻就是用户表达「我要用它」的时刻，此时把
 * 该用的额度填成套餐允许值，比拦下来让他自己去找那两个藏在「高级设置」里的输入框更省事。
 * 补的是**上限**而不是某个魔数，因为上限是这一档套餐已经卖给这个租户的值。
 *
 * 仍然为 0 的那格保持 0（比如管理员把套餐的 llm_daily 设成 0）：没有上限可补时不编数字，
 * 交给运行时说真话（20013「没有授权这项能力」）。
 */
export function fundLlmQuota(
  form: KeyPolicyInput,
  plan: Pick<ConsolePlanSnapshot, 'llm_qps' | 'llm_daily'> | null,
): KeyPolicyInput {
  if (plan === null) return form
  return {
    ...form,
    llm_qps: form.llm_qps > 0 ? form.llm_qps : plan.llm_qps,
    llm_daily: form.llm_daily > 0 ? form.llm_daily : plan.llm_daily,
  }
}
