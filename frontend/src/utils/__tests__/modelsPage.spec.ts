import { describe, expect, it } from 'vitest'

import {
  MODEL_ROLE_LABELS,
  RETIRED_MODEL_ROLES,
  referencedRolesOf,
  roleHint,
  roleOptions,
} from '../modelsPage'

/**
 * 模型配置页的两条产品规则，抽成纯函数后用 vitest 钉死：
 *
 * - 「不指向」必须是下拉里的一个可选项（空串），否则管理员没有办法把角色改回 `.env`；
 * - 「被谁引用」必须由**后端给的词表**过滤，前端不再自带一份角色清单——自带过一份
 *   `fast` / `strong`，而第 13 步把角色并成了 `default`，于是真正被引用的角色被静默丢掉。
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

describe('referencedRolesOf：按后端给的词表过滤', () => {
  it('顺序跟词表走，词表外的脏值丢掉', () => {
    expect(
      referencedRolesOf({ roles: ['strong', 'rerank', 'default'] }, ['default']),
    ).toEqual(['default'])
  })

  it('词表里有两个角色时按词表顺序，而不是按响应里的顺序', () => {
    expect(
      referencedRolesOf({ roles: ['b', 'a'] }, ['a', 'b']),
    ).toEqual(['a', 'b'])
  })

  it('没有任何角色引用时返回空列表（页面显示「—」）', () => {
    expect(referencedRolesOf({ roles: [] }, ['default'])).toEqual([])
  })

  it('大小写不同不算引用：角色名是精确枚举', () => {
    expect(referencedRolesOf({ roles: ['Default'] }, ['default'])).toEqual([])
  })
})

describe('角色文案', () => {
  it('default 的说明说清它是唯一角色、且不再提重排', () => {
    // 第 10 步票 3 起重排走专用端点（RERANK_BASE_URL / RERANK_MODEL），与模型角色无关。
    expect(MODEL_ROLE_LABELS.default).toContain('唯一')
    expect(MODEL_ROLE_LABELS.default).not.toContain('重排')
    expect(roleHint('default')).not.toContain('重排')
  })

  it('第 13 步撤销的两个角色仍能显示人话（旧数据 / 手工写库）', () => {
    for (const role of RETIRED_MODEL_ROLES) {
      expect(MODEL_ROLE_LABELS[role]).toContain('已撤销')
    }
  })

  it('词表外的角色不编解释：直接显示角色名本身', () => {
    expect(roleHint('some-future-role')).toBe('some-future-role')
  })
})

describe('referencedRolesOf 的兜底', () => {
  it('词表还没到（undefined）时返回空，而不是让整页崩', () => {
    // 真实故障：模板漏传第二个参数 → `.filter` of undefined → /admin/models 白屏。
    // 少一个角色胶囊可以接受，白屏不可以。
    expect(referencedRolesOf({ roles: ['default'] }, undefined)).toEqual([])
  })

  it('空词表同样是空列表（还没载入时不该假装「被引用」）', () => {
    expect(referencedRolesOf({ roles: ['default'] }, [])).toEqual([])
  })
})