import { describe, expect, it } from 'vitest'

import type { KeyPolicyData, PolicyForm } from '../../types/key'
import {
  buildPolicyPatch,
  disabledProfiles,
  policyFormFrom,
  toPolicyInput,
  visibleCustomOptions,
} from '../keyPolicy'

/**
 * 编辑器里三件「看起来是界面、其实是规则」的事：哪一档才显示手工参数、哪些档位
 * 被套餐挡住、保存时到底发哪几个字段。
 *
 * 为什么必须是纯函数：这三条都在 `KeyPolicyEditor.vue` 的模板里出现过一次就会变成
 * 「只能打开浏览器点一遍才能验」的规则；抽出来之后，`custom` 切走要卸载、套餐不支持的
 * 档位要置灰、部分更新不能把没改的字段也发出去，都能在 vitest 里钉死。
 * 期望值全部写成字面量，不复用被测函数的入参对象（否则断言只是回显）。
 */

const ORIGINAL: KeyPolicyData = {
  policy_id: 'policy_1',
  name: '集成 A',
  allowed_kb_ids: ['kb_1'],
  research_allowed: false,
  hybrid_allowed: true,
  rerank_allowed: false,
  query_rewrite_allowed: false,
  retrieve_qps: 10,
  retrieve_daily: 1000,
  profile: 'balanced',
  include_parent: true,
  expand_table_rows: true,
  custom_options: {},
}

describe('visibleCustomOptions', () => {
  it('只有 custom 档为真：切走即不再渲染那批手工参数', () => {
    expect(visibleCustomOptions('custom')).toBe(true)
    expect(visibleCustomOptions('speed')).toBe(false)
    expect(visibleCustomOptions('balanced')).toBe(false)
    expect(visibleCustomOptions('quality')).toBe(false)
    // 未知 / 空档位不猜：宁可整块不渲染，也不把不属于这档的输入框摆出来。
    expect(visibleCustomOptions('')).toBe(false)
    expect(visibleCustomOptions('enterprise')).toBe(false)
  })
})

describe('disabledProfiles', () => {
  it('套餐白名单之外的档位置灰，顺序按档位表的固定次序', () => {
    expect(disabledProfiles(['speed', 'balanced', 'custom'])).toEqual(['quality'])
    expect(disabledProfiles(['speed'])).toEqual(['balanced', 'quality', 'custom'])
  })

  it('白名单为空时四个档位全部置灰', () => {
    expect(disabledProfiles([])).toEqual(['speed', 'balanced', 'quality', 'custom'])
  })

  it('白名单里的未知档位不制造新的选项，只影响已知四档的判定', () => {
    expect(disabledProfiles(['speed', 'enterprise'])).toEqual(['balanced', 'quality', 'custom'])
  })
})

describe('buildPolicyPatch', () => {
  it('一个字段都没改时返回空对象：不把没动的字段也发出去', () => {
    const form: PolicyForm = policyFormFrom(ORIGINAL)

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({})
  })

  it('只改限额：补丁里只有 retrieve_qps', () => {
    const form = { ...policyFormFrom(ORIGINAL), retrieve_qps: 5 }

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({ retrieve_qps: 5 })
  })

  it('白名单只是顺序变了不算改动（集合语义），真加一个库才算', () => {
    const twoKbs: KeyPolicyData = { ...ORIGINAL, allowed_kb_ids: ['kb_1', 'kb_2'] }

    const reordered = {
      ...policyFormFrom(twoKbs),
      allowed_kb_ids: ['kb_2', 'kb_1'],
    }
    expect(buildPolicyPatch(reordered, twoKbs)).toEqual({})

    const widened = {
      ...policyFormFrom(twoKbs),
      allowed_kb_ids: ['kb_2', 'kb_1', 'kb_3'],
    }
    expect(buildPolicyPatch(widened, twoKbs)).toEqual({
      allowed_kb_ids: ['kb_2', 'kb_1', 'kb_3'],
    })
  })

  it('改档位时把随之打开的能力开关一并算进去', () => {
    const form = {
      ...policyFormFrom(ORIGINAL),
      profile: 'quality',
      rerank_allowed: true,
      query_rewrite_allowed: true,
    }

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({
      rerank_allowed: true,
      query_rewrite_allowed: true,
      profile: 'quality',
    })
  })

  it('custom 档只发真的改过的手工参数（部分合并，不整份替换）', () => {
    const customOriginal: KeyPolicyData = {
      ...ORIGINAL,
      profile: 'custom',
      custom_options: { mode: 'vector', top_k: 5, rerank: false, rewrite: false },
    }
    const form = {
      ...policyFormFrom(customOriginal),
      custom_options: {
        mode: 'vector',
        top_k: 8,
        rerank: false,
        rewrite: false,
      },
    }

    expect(buildPolicyPatch(form, customOriginal)).toEqual({
      custom_options: { top_k: 8 },
    })
  })

  it('从 custom 切走时不发 custom_options：命名档位是完整替换，这批键不生效', () => {
    const customOriginal: KeyPolicyData = {
      ...ORIGINAL,
      profile: 'custom',
      custom_options: { mode: 'hybrid', top_k: 8, rerank: true, rewrite: true },
    }
    const form = {
      ...policyFormFrom(customOriginal),
      profile: 'speed',
    }

    expect(buildPolicyPatch(form, customOriginal)).toEqual({ profile: 'speed' })
  })

  it('改结果形状只发对应的两个字段', () => {
    const form = { ...policyFormFrom(ORIGINAL), include_parent: false, expand_table_rows: false }

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({
      include_parent: false,
      expand_table_rows: false,
    })
  })
})

describe('policyFormFrom', () => {
  it('缺省的手工参数按「跟部署缺省走」的默认值补全，且不修改原对象', () => {
    const form = policyFormFrom(ORIGINAL)

    expect(form.custom_options).toEqual({
      mode: 'vector',
      top_k: 5,
      rerank: false,
      rewrite: false,
    })
    expect(ORIGINAL.custom_options).toEqual({})
    expect(form.allowed_kb_ids).toEqual(['kb_1'])
    // 复制而不是引用：编辑器改白名单时不能顺手改掉 query 缓存里那一行。
    expect(form.allowed_kb_ids).not.toBe(ORIGINAL.allowed_kb_ids)
  })
})

describe('toPolicyInput', () => {
  it('签发时给全量五组值；命名档位不带 custom_options 的内容', () => {
    const form = {
      ...policyFormFrom(ORIGINAL),
      profile: 'speed',
      custom_options: { mode: 'hybrid' as const, top_k: 8, rerank: true, rewrite: true },
    }

    expect(toPolicyInput(form)).toEqual({
      allowed_kb_ids: ['kb_1'],
      research_allowed: false,
      hybrid_allowed: true,
      rerank_allowed: false,
      query_rewrite_allowed: false,
      retrieve_qps: 10,
      retrieve_daily: 1000,
      profile: 'speed',
      include_parent: true,
      expand_table_rows: true,
      custom_options: {},
    })
  })

  it('custom 档才把手里的四个手工参数发出去', () => {
    const form = {
      ...policyFormFrom(ORIGINAL),
      profile: 'custom',
      custom_options: { mode: 'hybrid' as const, top_k: 8, rerank: true, rewrite: false },
    }

    expect(toPolicyInput(form).custom_options).toEqual({
      mode: 'hybrid',
      top_k: 8,
      rerank: true,
      rewrite: false,
    })
  })
})
