import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useRetrieveStore } from '../retrieve'

/**
 * 勾选了哪些库是**跨页面**的 UI 状态（01-frontend-conventions「命名与分层纪律」）：
 * 跳去建库 / 看列表再回来，选择不能丢，所以它必须住在 Pinia 里而不是页面 `ref` 里。
 * 这里测的是这个 store 自己承诺的语义，而不是某个组件怎么用它。
 */
describe('retrieve store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
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
