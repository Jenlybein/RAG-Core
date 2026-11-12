import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { getActiveKey, setActiveKey } from '../../lib/activeKey'
import { useRetrieveStore } from '../retrieve'

/**
 * 勾选了哪些库是**跨页面**的 UI 状态（01-frontend-conventions「命名与分层纪律」）：
 * 跳去建库 / 看列表再回来，选择不能丢，所以它必须住在 Pinia 里而不是页面 `ref` 里。
 * 这里测的是这个 store 自己承诺的语义，而不是某个组件怎么用它。
 *
 * 本票（#92）把「选哪把 Key」也放了进来：选中一把 Key 就是同时确定「请求层认哪把」
 * 与「哪些库在这把 Key 的白名单里」，所以换 Key 必须把不在新白名单里的已勾库移除——
 * 留着它们只会让下一次检索撞 20008，而错误发生在请求之后，用户看不到原因。
 */
describe('retrieve store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    // Key 是模块级内存，跨用例会残留；每个用例都从「还没选」开始。
    setActiveKey(null)
  })

  it('同一应用生命周期内复用同一个实例，选择不随页面重建', () => {
    const first = useRetrieveStore()
    first.setSelection(['kb_1'])

    // 页面切换时组件会重建，但再次调用的还是同一个 store —— 这就是「跨页保留」的基础。
    const second = useRetrieveStore()
    expect(second.selectedKbIds).toEqual(['kb_1'])
  })

  it('setSelection 去重，勾选数量如实', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_1', 'kb_2', 'kb_1'])

    expect(store.selectedKbIds).toEqual(['kb_1', 'kb_2'])
    expect(store.selectedCount).toBe(2)
  })

  it('?kb_id= 预填是「加上这个库」，不覆盖已有选择', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_old'])

    store.prefill('kb_from_url')

    expect(store.selectedKbIds).toEqual(['kb_old', 'kb_from_url'])
  })

  it('重复预填同一个库不会出现两次', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_old'])

    store.prefill('kb_old')

    expect(store.selectedKbIds).toEqual(['kb_old'])
  })
})

describe('retrieve store：选 Key 与白名单', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setActiveKey(null)
  })

  it('选中 Key：记下 id、把原文交给请求层，并保留白名单内的已勾库', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_1', 'kb_2'])

    const removed = store.selectKey({
      keyId: 'key_a',
      keyValue: 'rc_live_aaa',
      allowedKbIds: ['kb_1', 'kb_2', 'kb_3'],
    })

    expect(removed).toEqual([])
    expect(store.selectedKeyId).toBe('key_a')
    expect(store.selectedKbIds).toEqual(['kb_1', 'kb_2'])
    expect(store.allowedKbIds).toEqual(['kb_1', 'kb_2', 'kb_3'])
    expect(getActiveKey()).toBe('rc_live_aaa')
  })

  it('换 Key 时移除不在新白名单里的已勾库，并把这些 id 交回页面提示', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_1', 'kb_2', 'kb_3'])

    const removed = store.selectKey({
      keyId: 'key_b',
      keyValue: 'rc_live_bbb',
      allowedKbIds: ['kb_2'],
    })

    expect(removed).toEqual(['kb_1', 'kb_3'])
    expect(store.selectedKbIds).toEqual(['kb_2'])
    expect(store.selectedKeyId).toBe('key_b')
    expect(getActiveKey()).toBe('rc_live_bbb')
  })

  it('白名单为空（什么都没圈定）时，已勾库全部移除——空不等于「全部」', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_1'])

    const removed = store.selectKey({
      keyId: 'key_c',
      keyValue: 'rc_live_ccc',
      allowedKbIds: [],
    })

    expect(removed).toEqual(['kb_1'])
    expect(store.selectedKbIds).toEqual([])
  })

  it('清空 Key：请求层回到「还没选」，已勾库不跟着清（那是两件事）', () => {
    const store = useRetrieveStore()
    store.setSelection(['kb_1'])
    store.selectKey({ keyId: 'key_a', keyValue: 'rc_live_aaa', allowedKbIds: ['kb_1'] })

    store.clearKey()

    expect(store.selectedKeyId).toBeNull()
    expect(store.allowedKbIds).toEqual([])
    expect(store.selectedKbIds).toEqual(['kb_1'])
    expect(getActiveKey()).toBeNull()
  })

  it('isAllowed 只认当前白名单', () => {
    const store = useRetrieveStore()
    store.selectKey({ keyId: 'key_a', keyValue: 'rc_live_aaa', allowedKbIds: ['kb_1'] })

    expect(store.isAllowed('kb_1')).toBe(true)
    expect(store.isAllowed('kb_2')).toBe(false)
  })
})
