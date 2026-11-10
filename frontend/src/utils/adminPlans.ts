import type {
  PlanCapabilityField,
  PlanEditableFields,
  PlanImpact,
  PlanNumericField,
  PlanPatchBody,
} from '../types/consoleAdmin'
import { KEY_PROFILES, type KeyProfile } from '../types/key'

/**
 * 套餐页与账号页的判定规则，全部放在这里，不散在组件里。
 *
 * 为什么抽成纯函数：这些规则（影响面要不要确认、哪些档位允许被写、保存发哪些字段）
 * 是产品规则，不是渲染细节。放进 `PlansPage.vue` 就只能在浏览器里点一遍才能验，
 * 而这里用 vitest 直接钉死；组件只剩「什么时候调用、拿到结果怎么展示」。
 *
 * 一条贯穿全文的取舍：**影响面的数字一律来自 `count`**。后端的 `sample` 最多只放 5 个
 * 名字（`IMPACT_SAMPLE_LIMIT`），它是「快速扫一眼」的材料，不是计数；拿 `sample.length`
 * 当条件会在受影响对象更多、或样本恰好为空时漏掉弹窗，那正是这张票要防的事故。
 */

/**
 * 档位白名单的四个已知值。
 *
 * **名单只写在 `types/key.ts` 的 `KEY_PROFILES` 一处**（密钥配置的那份契约的真相），
 * 这里只 import 过来用，不再抄第二份——套餐页与 Key 页必须永远认同同一组档位名，
 * 多写一份就会在加档位时漏掉其中一处。
 */
export type ProfileCode = KeyProfile

/**
 * 六个数值上限的展示配置：面板上一字段一行，单位必须写出来。
 *
 * 后端只存数字、没有单位——「5」是每秒 5 次还是每天 5 次，只取决于列名，而列名不该
 * 让用户去后台查。单位写在这里，标签与字段名的对应关系也只有这一份。
 */
export interface PlanNumberField {
  readonly key: PlanNumericField
  readonly label: string
  readonly unit: string
}

export const PLAN_NUMBER_FIELDS: readonly PlanNumberField[] = [
  { key: 'retrieve_qps', label: '检索 QPS', unit: '次/秒' },
  { key: 'retrieve_daily', label: '日检索上限', unit: '次/天' },
  { key: 'max_kb', label: '知识库上限', unit: '个' },
  { key: 'max_kb_per_retrieve', label: '单次检索库数', unit: '个' },
  { key: 'max_documents_per_kb', label: '每库文档上限', unit: '个' },
  { key: 'max_processing_documents', label: '并发处理文档', unit: '个' },
]

/** 四个能力开关的展示配置；与六个上限一样，字段名到中文标签的映射只写这一处。 */
export interface PlanCapability {
  readonly key: PlanCapabilityField
  readonly label: string
}

export const PLAN_CAPABILITIES: readonly PlanCapability[] = [
  { key: 'hybrid_allowed', label: '混合检索' },
  { key: 'rerank_allowed', label: '重排' },
  { key: 'query_rewrite_allowed', label: '查询改写' },
  { key: 'research_allowed', label: '研究模式（Research）' },
]

/**
 * 从任意字符串数组里挑出四个已知档位。
 *
 * 两个作用：渲染勾选框时不被库里的脏值（手改库、旧枚举）带出一个不存在的勾选项；
 * 保存时也绝不把这些值发回后端（后端会判 20018「未知档位」）。
 * 输出按 `KEY_PROFILES` 的固定顺序，于是「集合相同、顺序不同」在 `buildPlanPatch` 里
 * 可以直接按数组比较，不会被误判成一次改动。
 */
export function pickKnownProfiles(values: readonly string[]): ProfileCode[] {
  return KEY_PROFILES.filter((code) => values.includes(code))
}

/**
 * 影响面是否值得停下来问一句。
 *
 * `policies_losing_profile` 单独判一次：它数的是密钥配置（比租户更细），后端正常时
 * 它非零必然伴随 `lose_profile` 非零，但这里不依赖那个不变量——确认弹窗宁可多弹一次，
 * 也不要因为「上游保证过」而漏掉一次会让客户调用开始报 20013 的保存。
 */
export function shouldConfirm(impact: PlanImpact | null | undefined): boolean {
  if (impact === null || impact === undefined) return false
  return (
    impact.over_max_kb.count > 0 ||
    impact.lose_profile.count > 0 ||
    impact.policies_losing_profile > 0
  )
}

/**
 * 确认弹窗的正文：只列真正非零的组，数字用 `count`，样本只作为「示例」附在后面。
 *
 * 返回 `string[]` 而不是拼好的一段 HTML：`ElMessageBox` 用纯文本渲染，样本里的租户名
 * 不会被当成 HTML 插入页面（租户名是用户可控的数据）。
 */
export function impactLines(impact: PlanImpact | null | undefined): string[] {
  if (impact === null || impact === undefined) return []

  const lines: string[] = []

  if (impact.over_max_kb.count > 0) {
    lines.push(
      `${impact.over_max_kb.count} 个租户的知识库数量会超过新的上限` +
        `（已检查 ${impact.tenants_checked} 个租户）` +
        sampleHint(impact.over_max_kb.sample),
    )
  }

  if (impact.lose_profile.count > 0) {
    lines.push(
      `${impact.lose_profile.count} 个租户的当前档位不在新的白名单里，` +
        `涉及 ${impact.policies_losing_profile} 条密钥配置` +
        sampleHint(impact.lose_profile.sample),
    )
  } else if (impact.policies_losing_profile > 0) {
    // 后端正常时走不到这里；万一试算只报了配置条数，也不能让弹窗正文空着。
    lines.push(`${impact.policies_losing_profile} 条密钥配置会失去当前档位`)
  }

  return lines
}

function sampleHint(sample: readonly string[]): string {
  if (sample.length === 0) return ''
  // 明确写「示例」：它是名单的开头几个，不是全部，也不是计数。
  return `；示例：${sample.join('、')}`
}

function sameProfiles(a: readonly ProfileCode[], b: readonly ProfileCode[]): boolean {
  // 两边都经 pickKnownProfiles 归一（固定顺序、去重），所以逐位比较就是集合比较。
  return a.length === b.length && a.every((profile, index) => profile === b[index])
}

/**
 * 把「原始行 + 编辑表单」折成一次 PATCH 的差异。
 *
 * 只发用户真正动过的字段：把整行发回去会让后端把没变的列也重写一遍（并写进审计），
 * 事后看账本会以为管理员逐项改过。返回空对象表示「没有改动」，页面据此不发请求。
 *
 * 名字为空时不上报：后端的 `min_length=1` 会把它判成 20002，而表单层已经拦了一次，
 * 这里再兜一次是为了让「清空名字点保存」不可能变成一次奇怪的请求。
 */
export function buildPlanPatch(
  before: PlanEditableFields,
  draft: PlanEditableFields,
): PlanPatchBody {
  const patch: PlanPatchBody = {}

  const name = draft.name.trim()
  if (name !== '' && name !== before.name) {
    patch.name = name
  }

  const nextProfiles = pickKnownProfiles(draft.allowed_profiles)
  const currentProfiles = pickKnownProfiles(before.allowed_profiles)
  if (!sameProfiles(nextProfiles, currentProfiles)) {
    patch.allowed_profiles = nextProfiles
  }

  for (const field of PLAN_CAPABILITIES) {
    if (draft[field.key] !== before[field.key]) {
      patch[field.key] = draft[field.key]
    }
  }

  for (const field of PLAN_NUMBER_FIELDS) {
    if (draft[field.key] !== before[field.key]) {
      patch[field.key] = draft[field.key]
    }
  }

  return patch
}
