import type { ModelConfigData, ModelRole } from '../types/modelConfig'
import type { SettingsImpact } from '../types/runtimeSettings'

/**
 * 模型配置页的判定与展示规则（纯函数）。
 *
 * 与 #95 的 `utils/adminPlans.ts` 同形：影响面用 `count`、展示文案在纯函数里生成、
 * 组件只负责「什么时候调用、拿到结果怎么展示」。这样「不指向」这个可选项、被引用角色
 * 的顺序、确认弹窗的条件都能用 vitest 直接钉死。
 */

/** 两个模型角色，顺序与后端 `app/core/model_roles.MODEL_ROLES` 一致（先快后准）。 */
export const MODEL_ROLES: readonly ModelRole[] = ['fast', 'strong']

/** 各角色的中文说明：面板上写清楚「它是干什么的」，而不是只给一个英文单词。 */
export const MODEL_ROLE_LABELS: Record<ModelRole, string> = {
  fast: 'fast（快速调用，同时承担重排）',
  strong: 'strong（需要思考的生成）',
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

/**
 * 这条配置现在被哪些角色引用。
 *
 * 只读后端返回的 `roles`（服务端已经算过「指针值 = 配置 id」），前端不自己猜；同时
 * 丢掉未知角色——`roles` 是裸字符串数组，手工写库或将来加角色时可能带着脏值，照着它
 * 渲染会看到一个不存在的角色标签。
 */
export function referencedRolesOf(config: Pick<ModelConfigData, 'roles'>): ModelRole[] {
  return MODEL_ROLES.filter((role) => config.roles.includes(role))
}

/**
 * 影响面是否值得停下来问一句：与 #95 的 `shouldConfirm` 同一形状。
 *
 * **一律用 `count`**。后端 `sample` 最多只放 5 个名字，拿它的长度判断会在受影响对象
 * 更多、或样本恰好为空时漏掉弹窗——那正是这张票要防的事故。
 */
export function shouldConfirmImpact(impact: SettingsImpact | null | undefined): boolean {
  if (impact === null || impact === undefined) return false
  return impact.quality_degraded.count > 0 || impact.keyword_channel_lost.count > 0
}

/**
 * 确认弹窗的正文：只列真正非零的组，数字用 `count`，样本作为「示例」附后。
 *
 * 返回字符串数组而不是拼好的 HTML：`ElMessageBox` 按纯文本渲染，租户名是用户可控的
 * 数据，不能被当成 HTML 插进页面。
 */
export function impactLines(impact: SettingsImpact | null | undefined): string[] {
  if (impact === null || impact === undefined) return []

  const lines: string[] = []

  if (impact.quality_degraded.count > 0) {
    lines.push(
      `${impact.quality_degraded.count} 个租户的 quality 档会少一步能力` +
        '（重排或改写被关掉）' +
        sampleHint(impact.quality_degraded.sample),
    )
  }

  if (impact.keyword_channel_lost.count > 0) {
    lines.push(
      `${impact.keyword_channel_lost.count} 个租户的 hybrid 档会失去关键词通道` +
        '（只剩向量一路召回）' +
        sampleHint(impact.keyword_channel_lost.sample),
    )
  }

  return lines
}

function sampleHint(sample: readonly string[]): string {
  if (sample.length === 0) return ''
  // 明确写「示例」：它是名单的前几个，不是全部，也不是计数。
  return `；示例：${sample.join('、')}`
}
