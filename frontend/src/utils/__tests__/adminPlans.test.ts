import { describe, expect, it } from 'vitest'

import type { PlanEditableFields, PlanImpact } from '../../types/consoleAdmin'
import { KEY_PROFILES } from '../../types/key'
import {
  buildPlanPatch,
  impactLines,
  pickKnownProfiles,
  shouldConfirm,
} from '../adminPlans'

/**
 * 这些用例钉住的是套餐页的三条产品规则，不碰渲染：
 *
 * - 「有没有影响面」必须靠 `count`，不能用 `sample.length` 判断（样本最多只列 5 个，
 *   3 个受影响时样本可能只有 2 个，甚至为空 —— 拿数组长度做条件会直接漏掉一次确认）；
 * - 档位白名单只有四个已知值，前端不提供自由输入，也绝不把未知值发给后端（那会变成 20018）；
 * - 编辑保存发的是**差异**，用户没动的字段不该被重写一遍。
 *
 * 组件只负责在什么时候调用它们，所以这里不需要挂载 Vue。
 */

function impactOf(overrides: Partial<PlanImpact> = {}): PlanImpact {
  return {
    tenants_checked: 0,
    over_max_kb: { count: 0, sample: [] },
    lose_profile: { count: 0, sample: [] },
    policies_losing_profile: 0,
    ...overrides,
  }
}

function planOf(overrides: Partial<PlanEditableFields> = {}): PlanEditableFields {
  return {
    name: '专业版',
    allowed_profiles: ['speed', 'balanced', 'quality', 'custom'],
    hybrid_allowed: true,
    rerank_allowed: true,
    query_rewrite_allowed: true,
    research_allowed: true,
    retrieve_qps: 50,
    retrieve_daily: 100000,
    max_kb: 50,
    max_kb_per_retrieve: 5,
    max_documents_per_kb: 5000,
    max_processing_documents: 10,
    ...overrides,
  }
}

describe('shouldConfirm：影响面弹窗要不要出现', () => {
  it('没有试算结果（没有 impact）时不弹', () => {
    expect(shouldConfirm(null)).toBe(false)
    expect(shouldConfirm(undefined)).toBe(false)
  })

  it('三组影响都为 0 时不弹，直接落库', () => {
    expect(shouldConfirm(impactOf())).toBe(false)
  })

  it('用 count 判定，不拿 sample 的长度：count=3 而 sample 为空也要弹确认', () => {
    // 这条是票面明确点名的坑：后备只把最多 5 个名字放进 sample，受影响对象更多时
    // sample.length 会小于 count；更极端的情况是样本恰好为空。
    expect(
      shouldConfirm(
        impactOf({ over_max_kb: { count: 3, sample: [] } }),
      ),
    ).toBe(true)
  })

  it('只要有一组非零就弹：失去档位的租户数也算', () => {
    expect(shouldConfirm(impactOf({ lose_profile: { count: 1, sample: [] } }))).toBe(true)
  })

  it('密钥配置会失去档位（policies_losing_profile > 0）同样要确认', () => {
    expect(shouldConfirm(impactOf({ policies_losing_profile: 2 }))).toBe(true)
  })
})

describe('impactLines：弹窗里给用户看的人话，数字一律来自 count', () => {
  it('没有 impact 或全为 0 时不产生任何一行', () => {
    expect(impactLines(null)).toEqual([])
    expect(impactLines(impactOf())).toEqual([])
  })

  it('超库数一栏用的是 count，不是样本条数', () => {
    const lines = impactLines(
      impactOf({ tenants_checked: 9, over_max_kb: { count: 4, sample: ['租户甲', '租户乙'] } }),
    )

    expect(lines.join('\n')).toContain('4 个租户')
    // 样本是“示例”而不是计数：样本只有 2 个名字，数字仍然是 4。
    expect(lines.join('\n')).toContain('租户甲')
    expect(lines.join('\n')).not.toContain('2 个租户')
  })

  it('失去档位同时点出受影响的密钥配置条数', () => {
    const lines = impactLines(
      impactOf({ lose_profile: { count: 2, sample: [] }, policies_losing_profile: 7 }),
    )
    const joined = lines.join('\n')

    expect(joined).toContain('2 个租户')
    expect(joined).toContain('7 条密钥配置')
  })

  it('只输出真正非零的那几组，避免满屏的「0 个」', () => {
    const lines = impactLines(impactOf({ over_max_kb: { count: 1, sample: [] } }))

    expect(lines).toHaveLength(1)
    expect(lines[0]).toContain('1 个租户')
  })
})

describe('pickKnownProfiles：allowed_profiles 只接受四个已知值', () => {
  it('丢掉未知值与重复值，并按固定顺序归一', () => {
    expect(pickKnownProfiles(['custom', 'admin', 'speed', 'custom'])).toEqual(['speed', 'custom'])
  })

  it('大小写不同也算未知值：档位名是精确枚举，不做模糊匹配', () => {
    expect(pickKnownProfiles(['Speed', 'QUALITY'])).toEqual([])
  })

  it('四个已知值按 speed/balanced/quality/custom 的顺序输出', () => {
    expect(pickKnownProfiles(['custom', 'quality', 'balanced', 'speed'])).toEqual([
      'speed',
      'balanced',
      'quality',
      'custom',
    ])
  })

  it('档位名单只来自 types/key.ts 的 KEY_PROFILES（后端 PROFILES 的那四个值）', () => {
    expect([...KEY_PROFILES]).toEqual(['speed', 'balanced', 'quality', 'custom'])
  })
})

describe('buildPlanPatch：只发差异，且绝不把未知档位写进请求', () => {
  it('什么都没改时返回空对象（页面据此提示「没有改动」而不是发一次空 PATCH）', () => {
    expect(buildPlanPatch(planOf(), planOf())).toEqual({})
  })

  it('名字去掉首尾空白后没变化就不算改动', () => {
    expect(buildPlanPatch(planOf(), planOf({ name: '  专业版  ' }))).toEqual({})
  })

  it('发出去的 allowed_profiles 只可能是四个已知值', () => {
    const patch = buildPlanPatch(
      planOf({ allowed_profiles: ['speed'] }),
      planOf({ allowed_profiles: ['speed', 'superuser', 'quality'] }),
    )

    expect(patch.allowed_profiles).toEqual(['speed', 'quality'])
    for (const profile of patch.allowed_profiles ?? []) {
      expect(KEY_PROFILES).toContain(profile)
    }
  })

  it('档位集合没变（只是顺序不同或有重复）不算改动', () => {
    expect(
      buildPlanPatch(
        planOf({ allowed_profiles: ['speed', 'quality'] }),
        planOf({ allowed_profiles: ['quality', 'speed', 'speed'] }),
      ),
    ).toEqual({})
  })

  it('数值与能力开关变了才出现在差异里，并带上该字段的原始值', () => {
    const patch = buildPlanPatch(
      planOf({ retrieve_qps: 50, max_kb: 50 }),
      planOf({ retrieve_qps: 30, max_kb: 20, rerank_allowed: false }),
    )

    expect(patch).toEqual({ retrieve_qps: 30, max_kb: 20, rerank_allowed: false })
  })
})
