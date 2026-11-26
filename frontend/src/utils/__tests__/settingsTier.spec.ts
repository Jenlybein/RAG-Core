import { describe, expect, it } from 'vitest'

import type { SettingRow } from '../../types/runtimeSettings'
import {
  EMBEDDING_BATCH_SIZE_KEY,
  RESTART_PANEL_WRITABLE_KEYS,
  RESTART_WORKER_NOTICE,
  embeddingBatchHintOf,
  groupByTier,
  isEditable,
  kindOf,
  restartNoticeOf,
  validateHotValue,
} from '../settingsTier'

/**
 * 这些用例钉的是检索参数页的渲染与保存规则，不碰组件：
 *
 * - 分组顺序必须跟后端给的行序（后端按 locked → restart → hot 排，前端不再排一次）；
 * - 某一档一行都没有时**不造空组**（否则页面上出现一个没有内容的标题，读者以为坏了）；
 * - locked 档绝不允许前端提交；restart 档只放行 `RESTART_PANEL_WRITABLE_KEYS` 点名的键
 *   （票 #224 之前是整档只读），数字/布尔的边界在发请求前拦住，省得把 20015 变成一次
 *   「看起来点了保存、其实后端拒了」的往返。
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
    minimum: null,
    maximum: null,
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

describe('isEditable：hot 档可写，locked 只读，restart 只放行点名的那一项', () => {
  it('locked 档一律只读', () => {
    // 即使后端将来把 editable 错报成 true，页面也不给写入口：这一档的 why 就是
    // 「能关掉鉴权」，前端不该把安全边界交给一个布尔字段。
    expect(isEditable(rowOf({ tier: 'locked', editable: false }))).toBe(false)
    expect(isEditable(rowOf({ tier: 'locked', editable: true }))).toBe(false)
  })

  it('restart 档里只有批大小可编辑（票 #224），其余仍只读', () => {
    // 验收原话：「该配置项在面板上可编辑，其余『需重启』项仍保持只读」。
    expect(
      isEditable(rowOf({ key: EMBEDDING_BATCH_SIZE_KEY, tier: 'restart', editable: true, value: 10 })),
    ).toBe(true)
    // 同一档的其它键：后端接受写库，但面板不给入口（改了要重建数据 / 重起整条链路）。
    for (const key of ['CHUNK_SIZE', 'CHUNK_OVERLAP', 'EMBEDDING_MODEL', 'EMBEDDING_DIMENSIONS']) {
      expect(isEditable(rowOf({ key, tier: 'restart', editable: true }))).toBe(false)
    }
  })

  it('例外是逐键名单，不是「restart 档整体可写」的开关', () => {
    // 名单本身也钉一下：把它扩成 .env 里那一整档，等于拆掉 #93/#96 的规则。
    expect(RESTART_PANEL_WRITABLE_KEYS).toEqual([EMBEDDING_BATCH_SIZE_KEY])
  })

  it('批大小这一行在后端 editable=false 时仍只读（后端收紧时页面跟着收紧）', () => {
    expect(
      isEditable(rowOf({ key: EMBEDDING_BATCH_SIZE_KEY, tier: 'restart', editable: false })),
    ).toBe(false)
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

  it('数字行按响应里给的边界拦越界，并把「当前值」写进原因', () => {
    // 边界来自后端那一行的 minimum / maximum（票 #224），前端不抄第三份表。
    const batch = rowOf({
      key: EMBEDDING_BATCH_SIZE_KEY,
      tier: 'restart',
      value: 10,
      minimum: 1,
      maximum: 25,
    })

    expect(validateHotValue(batch, 12)).toEqual({ ok: true, value: 12 })
    expect(validateHotValue(batch, 25)).toEqual({ ok: true, value: 25 })

    const tooSmall = validateHotValue(batch, 0)
    expect(tooSmall.ok).toBe(false)
    if (!tooSmall.ok) {
      expect(tooSmall.message).toContain('≥ 1')
      expect(tooSmall.message).toContain('当前 0')
    }

    const tooLarge = validateHotValue(batch, 26)
    expect(tooLarge.ok).toBe(false)
    if (!tooLarge.ok) {
      expect(tooLarge.message).toContain('≤ 25')
      expect(tooLarge.message).toContain('当前 26')
    }
  })

  it('没有声明边界的数字行仍接受 0（边界由后端白名单判定，前端不复述一张会漂移的表）', () => {
    expect(validateHotValue(rowOf({ value: 15 }), 0)).toEqual({ ok: true, value: 0 })
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

describe('批大小那一行的两句提示（票 #224）', () => {
  const batchRow = rowOf({
    key: EMBEDDING_BATCH_SIZE_KEY,
    tier: 'restart',
    value: 10,
    minimum: 1,
    maximum: 25,
  })

  it('提示里带上当前模型与它的单批上限，建议值就是那个上限', () => {
    const text = embeddingBatchHintOf(batchRow, { model: 'qwen3.7-text-embedding', limit: 20 })

    expect(text).toBe('当前模型 qwen3.7-text-embedding（单批上限 20），建议 ≤ 20')
  })

  it('别的行没有这句提示（模型只与嵌入批大小有关）', () => {
    expect(embeddingBatchHintOf(rowOf({ key: 'CHUNK_SIZE', tier: 'restart' }), { model: 'x', limit: 10 })).toBeNull()
    expect(embeddingBatchHintOf(rowOf({ tier: 'hot' }), { model: 'x', limit: 10 })).toBeNull()
  })

  it('后端没给素材时不给半句话（旧版后端 / 契约变更下的降级）', () => {
    expect(embeddingBatchHintOf(batchRow, null)).toBeNull()
    expect(embeddingBatchHintOf(batchRow, undefined)).toBeNull()
    expect(embeddingBatchHintOf(batchRow, { model: '', limit: 10 })).toBeNull()
    expect(embeddingBatchHintOf(batchRow, { model: 'x', limit: 0 })).toBeNull()
  })

  it('可编辑的「需重启」行旁边写「保存后需重启 Worker 生效」', () => {
    expect(restartNoticeOf(batchRow)).toBe(RESTART_WORKER_NOTICE)
    expect(RESTART_WORKER_NOTICE).toContain('重启 Worker')
  })

  it('只读的「需重启」行、热更行与只读行都不挂这句（没有保存动作可提醒）', () => {
    expect(restartNoticeOf(rowOf({ key: 'CHUNK_SIZE', tier: 'restart' }))).toBeNull()
    expect(restartNoticeOf(rowOf({ tier: 'hot' }))).toBeNull()
    expect(restartNoticeOf(rowOf({ tier: 'locked', editable: false }))).toBeNull()
  })
})
