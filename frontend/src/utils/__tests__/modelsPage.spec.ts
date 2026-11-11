import { describe, expect, it } from 'vitest'

import { referencedRolesOf, roleOptions } from '../modelsPage'

/**
 * 模型配置页的两条产品规则，抽成纯函数后用 vitest 钉死：
 *
 * - 「不指向」必须是下拉里的一个可选项（空串），否则管理员没有办法把角色改回 `.env`；
 * - 删除仍被引用的配置由后端的 20015 兜底，但列表上「被谁引用」必须只来自后端的
 *   `roles` 字段，不在前端自己猜指针。
 *
 * 影响面预览（`shouldConfirmImpact` / `impactLines`）已移到 `utils/impactPreview.ts`，
 * 对应用例在 `impactPreview.spec.ts`。
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
