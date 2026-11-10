import { describe, expect, it } from 'vitest'

import type { SettingRow } from '../../types/runtimeSettings'
import { groupByTier, isEditable, kindOf, validateHotValue } from '../settingsTier'

/**
 * 这些用例钉的是检索参数页的渲染与保存规则，不碰组件：
 *
 * - 分组顺序必须跟后端给的行序（后端按 locked → restart → hot 排，前端不再排一次）；
 * - 某一档一行都没有时**不造空组**（否则页面上出现一个没有内容的标题，读者以为坏了）；
 * - locked 与 restart 两档都绝不允许前端提交（restart 档在面板上只读，改它走运维流程），
 *   数字/布尔的边界在发请求前拦住，省得把 20015 变成一次「看起来点了保存、其实后端拒了」
 *   的往返。
 */

function rowOf(overrides: Partial<SettingRow> = {}): SettingRow {
  return {
    key: 'TOP_K',
    tier: 'hot',
    editable: true,
    value: 5,
    source: 'env',
    invalid: false,
    why: '缺省值是给 custom 档回落用的',
    invalid_reason: null,
    ...overrides,
  }
}

describe('groupByTier：顺序照后端，缺档不造空组', () => {
  it('保留行序与首次出现的档序，不按档位名重排', () => {
    const rows = [
      rowOf({ key: 'A', tier: 'restart' }),
      rowOf({ key: 'B', tier: 'hot' }),
      rowOf({ key: 'C', tier: 'restart' }),
      rowOf({ key: 'D', tier: 'locked' }),
    ]

    const groups = groupByTier(rows)

    // 组的顺序是「restart 先出现」而不是 locked/restart/hot 的字典序：后端已经排好了。
    expect(groups.map((group) => group.tier)).toEqual(['restart', 'hot', 'locked'])
    expect(groups[0].rows.map((row) => row.key)).toEqual(['A', 'C'])
    expect(groups[1].rows.map((row) => row.key)).toEqual(['B'])
  })

  it('没有行的档位不产生空组', () => {
    const groups = groupByTier([rowOf({ tier: 'hot' })])

    expect(groups).toHaveLength(1)
    expect(groups[0].tier).toBe('hot')
  })

  it('空输入返回空列表（页面据此不渲染任何分组）', () => {
    expect(groupByTier([])).toEqual([])
  })

  it('不修改入参里的行对象', () => {
    const original = [rowOf({ tier: 'hot' }), rowOf({ tier: 'hot', key: 'TOP_K2' })]
    groupByTier(original)

    expect(original).toHaveLength(2)
    expect(original[1].key).toBe('TOP_K2')
  })
})

describe('isEditable：只有 hot 档可写，locked / restart 在面板上只读', () => {
  it('locked 档一律只读', () => {
    // 即使后端将来把 editable 错报成 true，页面也不给写入口：这一档的 why 就是
    // 「能关掉鉴权」，前端不该把安全边界交给一个布尔字段。
    expect(isEditable(rowOf({ tier: 'locked', editable: false }))).toBe(false)
    expect(isEditable(rowOf({ tier: 'locked', editable: true }))).toBe(false)
  })

  it('restart 档即使后端 editable=true 也只读：#93/#96 要求面板不可编辑', () => {
    // 后端接受 restart 档的写库请求（它只表达「把意图记下来」），但验收要求面板上
    // 不给入口，改它得走运维流程；保存时因此也不能出现 restart 的改动。
    expect(isEditable(rowOf({ tier: 'restart', editable: true }))).toBe(false)
    expect(isEditable(rowOf({ tier: 'restart', editable: false }))).toBe(false)
  })

  it('hot 档仍按后端的 editable 决定', () => {
    expect(isEditable(rowOf({ tier: 'hot', editable: true }))).toBe(true)
    expect(isEditable(rowOf({ tier: 'hot', editable: false }))).toBe(false)
  })
})

describe('kindOf：按有效值类型推断输入控件，角色指针单独按字符串', () => {
  it('布尔 / 数字 / 字符串各归各', () => {
    expect(kindOf(rowOf({ key: 'A', value: true }))).toBe('boolean')
    expect(kindOf(rowOf({ key: 'B', value: 3 }))).toBe('number')
    expect(kindOf(rowOf({ key: 'C', value: 'hybrid' }))).toBe('string')
  })

  it('角色指针的值是 null 时仍按字符串（它的输入是一段 id 文本）', () => {
    expect(kindOf(rowOf({ key: 'model.role.fast', value: null }))).toBe('string')
  })
})

describe('validateHotValue：发请求前的类型与边界', () => {
  it('字符串行接受任意字符串（空串也有含义：base_url 用默认地址、指针表示不指向）', () => {
    const row = rowOf({ key: 'LLM_BASE_URL', value: '' })

    expect(validateHotValue(row, '')).toEqual({ ok: true, value: '' })
    expect(validateHotValue(row, 'https://api.example.com')).toEqual({
      ok: true,
      value: 'https://api.example.com',
    })
    expect(validateHotValue(row, 5).ok).toBe(false)
  })

  it('布尔行只收真布尔，不收 "true" / 1 这类会被 pydantic 宽松转换的值', () => {
    const row = rowOf({ key: 'RERANK_ENABLED', value: true })

    expect(validateHotValue(row, false)).toEqual({ ok: true, value: false })
    expect(validateHotValue(row, 'true').ok).toBe(false)
    expect(validateHotValue(row, 1).ok).toBe(false)
  })

  it('数字行拒绝 NaN / Infinity / 负值，接受 0（下界由后端白名单判定，前端不复述一张会漂移的表）', () => {
    const row = rowOf({ key: 'RERANK_TIMEOUT_SECONDS', value: 15.0 })

    expect(validateHotValue(row, 9.5)).toEqual({ ok: true, value: 9.5 })
    expect(validateHotValue(row, 0)).toEqual({ ok: true, value: 0 })
    expect(validateHotValue(row, Number.NaN).ok).toBe(false)
    expect(validateHotValue(row, Number.POSITIVE_INFINITY).ok).toBe(false)
    expect(validateHotValue(row, -1).ok).toBe(false)
    expect(validateHotValue(row, '9').ok).toBe(false)
  })

  it('数字行接受小数：是否为整数由后端按字段类型判定，前端不复述那张类型表', () => {
    expect(validateHotValue(rowOf({ value: 5 }), 3.5).ok).toBe(true)
    expect(validateHotValue(rowOf({ value: 5 }), 3).ok).toBe(true)
  })

  it('角色指针行的值是 null 时按字符串处理，空串是「不指向」这个明确状态', () => {
    const row = rowOf({ key: 'model.role.fast', value: null })

    expect(validateHotValue(row, '').ok).toBe(true)
    expect(validateHotValue(row, 'cfg-1')).toEqual({ ok: true, value: 'cfg-1' })
    expect(validateHotValue(row, 1).ok).toBe(false)
  })

  it('不可写的行直接拒绝，消息里说明原因', () => {
    const result = validateHotValue(rowOf({ tier: 'locked', editable: false }), 5)

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toContain('不可')
  })
})
