import type { ModelConfigData } from '../types/modelConfig'

/**
 * **模型配置页专用**的判定与展示规则（纯函数）。
 *
 * 影响面预览（`shouldConfirmImpact` / `impactLines`）已移到 `utils/impactPreview.ts`：
 * 它也被检索参数页使用，不属于本页。这里只剩真正与模型接入相关的三件事——角色下拉、
 * 被引用角色、出厂供应商表。
 *
 * 与 #95 的 `utils/adminPlans.ts` 同形：展示文案在纯函数里生成、组件只负责
 * 「什么时候调用、拿到结果怎么展示」，这样「不指向」这个可选项与被引用角色的顺序
 * 都能用 vitest 直接钉死。
 */

/**
 * 各角色的中文说明：面板上写清楚「它是干什么的」，而不是只给一个英文单词。
 *
 * **词表不在前端**：角色清单由 `GET /console/model-configs` 的 `roles` 给（后端
 * `app/core/model_roles.MODEL_ROLES` 是唯一出处）。这里只剩「某个角色叫什么」，查不到
 * 就退回角色名本身——将来后端加角色，面板仍然能用，只是少了那句中文。
 */
export const MODEL_ROLE_LABELS: Record<string, string> = {
  default: 'default（平台唯一的模型角色：改写 / 分解 / Research 都用它）',
  // 第 13 步之前的两档角色。留着是为了在旧响应 / 手工写库时仍能显示人话，
  // 但它们**不在候选词表里**了：面板不会再让人选它们。
  fast: 'fast（已撤销：第 13 步并入 default）',
  strong: 'strong（已撤销：第 13 步并入 default）',
}

/** 角色的一行说明；词表里没有的角色就显示角色名本身，不编一句解释。 */
export function roleHint(role: string): string {
  const label = MODEL_ROLE_LABELS[role]
  if (label === undefined) return role
  const parts = label.split('（')
  return parts.length > 1 ? parts[1].replace('）', '') : label
}

/**
 * 已撤销的两个角色。**只用于给旧数据打标签**，不参与候选词表——`RolesGate` 的下拉
 * 只渲染后端给的那一份。
 */
export const RETIRED_MODEL_ROLES: readonly string[] = ['fast', 'strong']

/**
 * 这条配置现在被哪些角色引用（按 `knownRoles` 的顺序，丢掉词表外的脏值）。
 *
 * `knownRoles` 来自 `GET /console/model-configs` 的 `roles`：顺序即展示顺序，前端不再
 * 自带一份。旧实现拿硬编码的 `['fast','strong']` 过滤，于是 `default` 被静默丢掉，
 * 面板显示「没有被引用」——那是错的。
 */
export function referencedRolesOf(
  config: Pick<ModelConfigData, 'roles'>,
  knownRoles: readonly string[],
): string[] {
  return knownRoles.filter((role) => config.roles.includes(role))
}

/**
 * 六个出厂供应商，与后端 `app/core/llm_vendors.py` 的 `LLM_VENDORS` 逐字一致。
 *
 * 为什么前端也有一份：供应商是**写入时**的校验（后端拒掉不在注册表里的名字），而这
 * 张表要在新增表单里做成下拉。没有「列出供应商」的接口，前端只能持有这份常量；任何
 * 新增供应商都要同时改两处——这是本页唯一一处已知的重复真相，写在这里以免被当成漏改。
 *
 * 「自定义端点」不是第七个供应商：选一个供应商、把 `base_url` 填成自建网关的地址即可
 * （后端按名字索引装配知识，任意字符串会让第一次调用才 KeyError）。
 */
export const MODEL_VENDORS: readonly string[] = [
  'deepseek',
  'qwen',
  'ark',
  'openai',
  'anthropic',
  'gemini',
]

/** 供应商的中文名只用于展示；发出去的值仍是上面的英文标识。 */
const VENDOR_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  qwen: '通义千问',
  ark: '火山方舟',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  gemini: 'Gemini',
}

export function vendorLabel(provider: string): string {
  return VENDOR_LABELS[provider] ?? provider
}

/** 「不指向」这一项的值：空串。后端把空串规范成「不指向，回落 `.env`」。 */
export const NO_ROLE_TARGET = ''

/** 角色下拉的一个选项。 */
export interface RoleOption {
  readonly value: string
  readonly label: string
}

/** 角色下拉只要这三样东西：下拉选项的「值」是 id，展示用备注名 + 模型名。 */
export type RoleOptionSource = Pick<ModelConfigData, 'id' | 'label' | 'model'>

/**
 * 角色下拉的选项：第一项永远是「不指向（回落 `.env`）」，其余是现存配置。
 *
 * 空项必须存在：它是管理员**改回 `.env` 单供应商**的唯一入口，也是「清空指向」这个
 * 动作本身。把它藏起来等于让人无法撤销一次指向。
 */
export function roleOptions(configs: readonly RoleOptionSource[]): RoleOption[] {
  return [
    { value: NO_ROLE_TARGET, label: '不指向（回落 .env）' },
    ...configs.map((config) => ({
      value: config.id,
      label: `${config.label}（${config.model}）`,
    })),
  ]
}

