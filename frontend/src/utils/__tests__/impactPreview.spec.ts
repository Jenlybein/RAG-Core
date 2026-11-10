import { describe, expect, it } from 'vitest'

import type { SettingsImpact } from '../../types/runtimeSettings'
import { impactLines, shouldConfirmImpact } from '../impactPreview'

/**
 * 通用影响面预览的两条规则，抽成纯函数后用 vitest 钉死：
 *
 * - 判定一律用 `count`，与 #95 的套餐影响预览同一形状（`sample` 最多 5 个名字，
 *   拿它当计数会漏掉确认弹窗）；
 * - 弹窗正文只列真正非零的组，样本只作为「示例」附在后面。
 *
 * 这两个函数原先与模型配置页的规则混在 `modelsPage.spec.ts` 里，现在按模块拆开：
 * 测试文件名应当说明它在钉哪一份实现。
 */
describe('shouldConfirmImpact：用 count 判断，不看 sample 长度', () => {
  function impactOf(overrides: Partial<SettingsImpact> = {}): SettingsImpact {
    return {
      quality_degraded: { count: 0, sample: [] },
      keyword_channel_lost: { count: 0, sample: [] },
      ...overrides,
    }
  }

  it('没有试算结果时不弹', () => {
    expect(shouldConfirmImpact(null)).toBe(false)
    expect(shouldConfirmImpact(undefined)).toBe(false)
  })

  it('两组都为 0 时不弹', () => {
    expect(shouldConfirmImpact(impactOf())).toBe(false)
  })

  it('count 非零、sample 为空也必须弹（sample 只是示例，不是计数）', () => {
    expect(
      shouldConfirmImpact(
        impactOf({ quality_degraded: { count: 3, sample: [] } }),
      ),
    ).toBe(true)
    expect(
      shouldConfirmImpact(
        impactOf({ keyword_channel_lost: { count: 1, sample: [] } }),
      ),
    ).toBe(true)
  })
})

describe('impactLines：弹窗正文里的数字全部来自 count', () => {
  it('只列非零组，样本标成「示例」', () => {
    const lines = impactLines({
      quality_degraded: { count: 4, sample: ['租户甲', '租户乙'] },
      keyword_channel_lost: { count: 2, sample: [] },
    })

    const joined = lines.join('\n')
    expect(joined).toContain('4 个租户')
    expect(joined).toContain('2 个租户')
    expect(joined).toContain('示例：租户甲、租户乙')
    // 「质量退化」按 count=4 报，而不是按样本的 2 个名字报。
    expect(joined).toContain('4 个租户的 quality 档会少一步能力')
  })

  it('没有影响或全为 0 时不产生任何一行', () => {
    expect(impactLines(null)).toEqual([])
    const zero = {
      quality_degraded: { count: 0, sample: [] },
      keyword_channel_lost: { count: 0, sample: [] },
    }
    expect(impactLines(zero)).toEqual([])
  })
})
