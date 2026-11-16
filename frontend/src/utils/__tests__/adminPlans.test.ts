import { describe, expect, it } from 'vitest'

import type { PlanEditableFields, PlanImpact } from '../../types/consoleAdmin'
import { buildPlanPatch, impactLines, shouldConfirm } from '../adminPlans'

/**
 * 这些用例钉住的是套餐页的三条产品规则，不碰渲染：
 *
 * - 「有没有影响面」必须靠 `count`，不能用 `sample.length` 判断（样本最多只列 5 个，
 *   3 个受影响时样本可能只有 2 个，甚至为空 —— 拿数组长度做条件会直接漏掉一次确认）；
 * - 影响面的第二组是「超出新授权范围」（第 13 步起没有档位白名单，改的是五个授权位）；
 * - 编辑保存发的是**差异**，用户没动的字段不该被重写一遍；0 是真实额度，不能被吞掉。
 *
 * 组件只负责在什么时候调用它们，所以这里不需要挂载 Vue。
 */

function impactOf(overrides: Partial<PlanImpact> = {}): PlanImpact {
  return {
    tenants_checked: 0,
    over_max_kb: { count: 0, sample: [] },
    over_capability: { count: 0, sample: [] },
    policies_over_capability: 0,
    ...overrides,
  }
}

function planOf(overrides: Partial<PlanEditableFields> = {}): PlanEditableFields {
  return {
    name: '专业版',
    retrieve_allowed: true,
    rewrite_allowed: true,
    decompose_allowed: true,
    research_allowed: true,
    rerank_allowed: true,
    retrieve_qps: 50,
    retrieve_daily: 25000,
    rerank_qps: 15,
    rerank_daily: 5000,
    llm_qps: 5,
    llm_daily: 1500,
    max_top_k: 15,
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
    // 这条是票面明确点名的坑：后端只把最多 5 个名字放进 sample，受影响对象更多时
    // sample.length 会小于 count；更极端的情况是样本恰好为空。
    expect(
      shouldConfirm(
        impactOf({ over_max_kb: { count: 3, sample: [] } }),
      ),
    ).toBe(true)
  })

  it('只要有一组非零就弹：超出新授权范围的租户数也算', () => {
    expect(shouldConfirm(impactOf({ over_capability: { count: 1, sample: [] } }))).toBe(true)
  })

  it('密钥配置会超出授权（policies_over_capability > 0）同样要确认', () => {
    expect(shouldConfirm(impactOf({ policies_over_capability: 2 }))).toBe(true)
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

  it('超出授权范围同时点出受影响的密钥配置条数', () => {
    const lines = impactLines(
      impactOf({ over_capability: { count: 2, sample: [] }, policies_over_capability: 7 }),
    )
    const joined = lines.join('\n')

    expect(joined).toContain('2 个租户')
    expect(joined).toContain('7 条密钥配置')
  })

  it('只报了配置条数时正文也不空着（不依赖上游不变量）', () => {
    const lines = impactLines(impactOf({ policies_over_capability: 3 }))

    expect(lines).toHaveLength(1)
    expect(lines[0]).toContain('3 条密钥配置')
  })

  it('只输出真正非零的那几组，避免满屏的「0 个」', () => {
    const lines = impactLines(impactOf({ over_max_kb: { count: 1, sample: [] } }))

    expect(lines).toHaveLength(1)
    expect(lines[0]).toContain('1 个租户')
  })
})

describe('buildPlanPatch：只发差异', () => {
  it('什么都没改时返回空对象（页面据此提示「没有改动」而不是发一次空 PATCH）', () => {
    expect(buildPlanPatch(planOf(), planOf())).toEqual({})
  })

  it('名字去掉首尾空白后没变化就不算改动', () => {
    expect(buildPlanPatch(planOf(), planOf({ name: '  专业版  ' }))).toEqual({})
  })

  it('数值与授权位变了才出现在差异里，并带上该字段的原始值', () => {
    const patch = buildPlanPatch(
      planOf({ retrieve_qps: 50, max_kb: 50 }),
      planOf({ retrieve_qps: 30, max_kb: 20, rerank_allowed: false }),
    )

    expect(patch).toEqual({ retrieve_qps: 30, max_kb: 20, rerank_allowed: false })
  })

  it('加工额度从有调回 0 是真实改动（0 = 这项能力没买，不被吞掉）', () => {
    const patch = buildPlanPatch(
      planOf(),
      planOf({ rerank_qps: 0, rerank_daily: 0, llm_qps: 0, llm_daily: 0 }),
    )

    expect(patch).toEqual({ rerank_qps: 0, rerank_daily: 0, llm_qps: 0, llm_daily: 0 })
  })

  it('max_top_k 调成 0（不额外封顶）也算改动', () => {
    expect(buildPlanPatch(planOf(), planOf({ max_top_k: 0 }))).toEqual({ max_top_k: 0 })
  })
})