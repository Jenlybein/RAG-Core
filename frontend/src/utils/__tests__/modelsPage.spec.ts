import { describe, expect, it } from 'vitest'

import type { SettingsImpact } from '../../types/runtimeSettings'
import { impactLines, referencedRolesOf, roleOptions, shouldConfirmImpact } from '../modelsPage'

/**
 * 模型配置页的两条产品规则，抽成纯函数后用 vitest 钉死：
 *
 * - 「不指向」必须是下拉里的一个可选项（空串），否则管理员没有办法把角色改回 `.env`；
 * - 删除仍被引用的配置由后端的 20015 兜底，但列表上「被谁引用」必须只来自后端的
 *   `roles` 字段，不在前端自己猜指针；
 * - 影响面判定一律用 `count`，与 #95 的套餐影响预览同一形状（`sample` 最多 5 个名字，
 *   拿它当计数会漏掉确认弹窗）。
 */

describe('roleOptions：下拉的第一个选项是「不指向」', () => {
  it('第一项用空串表示「不指向（回落 .env）」', () => {
    const options = roleOptions([])

    expect(options).toHaveLength(1)
    expect(options[0].value).toBe('')
    expect(options[0].label).toContain('不指向')
  })

  it('配置项按入参顺序追加，值和标签都带上 id 与模型名', () => {
    const options = roleOptions([
      { id: 'cfg-a', label: '生产', model: 'deepseek-v4-pro' },
      { id: 'cfg-b', label: '备用', model: 'qwen-max' },
    ])

    expect(options.map((option) => option.value)).toEqual(['', 'cfg-a', 'cfg-b'])
    expect(options[1].label).toContain('生产')
    expect(options[1].label).toContain('deepseek-v4-pro')
  })
})

describe('referencedRolesOf：只认 fast / strong，顺序与后端一致', () => {
  it('按 fast、strong 的顺序返回，丢掉未知角色', () => {
    expect(referencedRolesOf({ roles: ['strong', 'rerank', 'fast'] })).toEqual(['fast', 'strong'])
  })

  it('没有任何角色引用时返回空列表（页面显示「—」）', () => {
    expect(referencedRolesOf({ roles: [] })).toEqual([])
  })

  it('大小写不同不算引用：角色名是精确枚举', () => {
    expect(referencedRolesOf({ roles: ['Fast', 'STRONG'] })).toEqual([])
  })
})

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
