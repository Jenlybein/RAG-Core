import { describe, expect, it } from 'vitest'

import { PARAM_PRESETS, type KeyPolicyData, type PolicyForm } from '../../types/key'
import { applyParamPreset, buildPolicyPatch, policyFormFrom, toPolicyInput } from '../keyPolicy'

/**
 * 编辑器里几件「看起来是界面、其实是规则」的事：表单怎么从线上配置拷出来、
 * 保存时到底发哪几个字段、预设按钮填哪三项。
 *
 * 为什么必须是纯函数：这些规则在 `KeyPolicyEditor.vue` 的模板里出现过一次就会变成
 * 「只能打开浏览器点一遍才能验」的规则；抽出来之后，部分更新不能把没改的字段也发出去、
 * 白名单按集合比较、0 是真实额度而不是「没填」，都能在 vitest 里钉死。
 * 期望值全部写成字面量，不复用被测函数的入参对象（否则断言只是回显）。
 */

const ORIGINAL: KeyPolicyData = {
  policy_id: 'policy_1',
  name: '集成 A',
  allowed_kb_ids: ['kb_1'],
  retrieve_allowed: true,
  rewrite_allowed: false,
  decompose_allowed: false,
  research_allowed: false,
  rerank_allowed: false,
  retrieve_qps: 10,
  retrieve_daily: 1000,
  rerank_qps: 0,
  rerank_daily: 0,
  llm_qps: 0,
  llm_daily: 0,
  retrieval_mode: 'hybrid',
  retrieval_top_k: 5,
  retrieval_rerank: false,
  include_parent: true,
  expand_table_rows: true,
}

describe('policyFormFrom', () => {
  it('照抄全部配置字段，但不带 policy_id / name（它们进不了签发请求）', () => {
    const form = policyFormFrom(ORIGINAL)

    expect(form).toEqual({
      allowed_kb_ids: ['kb_1'],
      retrieve_allowed: true,
      rewrite_allowed: false,
      decompose_allowed: false,
      research_allowed: false,
      rerank_allowed: false,
      retrieve_qps: 10,
      retrieve_daily: 1000,
      rerank_qps: 0,
      rerank_daily: 0,
      llm_qps: 0,
      llm_daily: 0,
      retrieval_mode: 'hybrid',
      retrieval_top_k: 5,
      retrieval_rerank: false,
      include_parent: true,
      expand_table_rows: true,
    })
  })

  it('白名单是拷贝而不是引用，且不修改原对象', () => {
    const form = policyFormFrom(ORIGINAL)

    expect(form.allowed_kb_ids).toEqual(['kb_1'])
    expect(form.allowed_kb_ids).not.toBe(ORIGINAL.allowed_kb_ids)
    form.allowed_kb_ids.push('kb_2')
    expect(ORIGINAL.allowed_kb_ids).toEqual(['kb_1'])
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

  it('打开一个授权位只发那一个位', () => {
    const form = { ...policyFormFrom(ORIGINAL), rerank_allowed: true }

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({ rerank_allowed: true })
  })

  it('三项直参数各自独立可改：只动 top_k 就只发 top_k', () => {
    const form = { ...policyFormFrom(ORIGINAL), retrieval_top_k: 8 }

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({ retrieval_top_k: 8 })
  })

  it('加工额度从 0 调大、或从有调回 0，都是真实改动（0 不被吞掉）', () => {
    const opened = { ...policyFormFrom(ORIGINAL), rerank_qps: 3, rerank_daily: 100 }
    expect(buildPolicyPatch(opened, ORIGINAL)).toEqual({ rerank_qps: 3, rerank_daily: 100 })

    const withQuota: KeyPolicyData = { ...ORIGINAL, llm_qps: 2, llm_daily: 50 }
    const closed = { ...policyFormFrom(withQuota), llm_qps: 0, llm_daily: 0 }
    expect(buildPolicyPatch(closed, withQuota)).toEqual({ llm_qps: 0, llm_daily: 0 })
  })

  it('改结果形状只发对应的两个字段', () => {
    const form = { ...policyFormFrom(ORIGINAL), include_parent: false, expand_table_rows: false }

    expect(buildPolicyPatch(form, ORIGINAL)).toEqual({
      include_parent: false,
      expand_table_rows: false,
    })
  })
})

describe('toPolicyInput', () => {
  it('签发时给全量值，白名单去重去空串，不带 policy_id / name', () => {
    const form = {
      ...policyFormFrom(ORIGINAL),
      allowed_kb_ids: ['kb_1', ' kb_1 ', '', 'kb_2'],
    }

    expect(toPolicyInput(form)).toEqual({
      allowed_kb_ids: ['kb_1', 'kb_2'],
      retrieve_allowed: true,
      rewrite_allowed: false,
      decompose_allowed: false,
      research_allowed: false,
      rerank_allowed: false,
      retrieve_qps: 10,
      retrieve_daily: 1000,
      rerank_qps: 0,
      rerank_daily: 0,
      llm_qps: 0,
      llm_daily: 0,
      retrieval_mode: 'hybrid',
      retrieval_top_k: 5,
      retrieval_rerank: false,
      include_parent: true,
      expand_table_rows: true,
    })
  })
})

describe('applyParamPreset', () => {
  it('quality 预设填好三项直参数，不碰授权位与额度，返回新对象', () => {
    const form = policyFormFrom(ORIGINAL)
    const quality = PARAM_PRESETS.find((preset) => preset.name === 'quality')
    expect(quality).toBeDefined()

    const next = applyParamPreset(form, quality!)

    expect(next.retrieval_mode).toBe('hybrid')
    expect(next.retrieval_top_k).toBe(5)
    expect(next.retrieval_rerank).toBe(true)
    // 参数想要重排 ≠ 授权允许重排：授权位保持原样，由用户自己打开。
    expect(next.rerank_allowed).toBe(false)
    expect(next.retrieve_qps).toBe(10)
    expect(next).not.toBe(form)
    expect(form.retrieval_rerank).toBe(false)
  })

  it('speed 预设把混合检索填回向量、关掉重排参数', () => {
    const form = { ...policyFormFrom(ORIGINAL), retrieval_rerank: true }
    const speed = PARAM_PRESETS.find((preset) => preset.name === 'speed')
    expect(speed).toBeDefined()

    const next = applyParamPreset(form, speed!)

    expect(next.retrieval_mode).toBe('vector')
    expect(next.retrieval_rerank).toBe(false)
  })
})