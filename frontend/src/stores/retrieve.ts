import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { setActiveKey } from '../lib/activeKey'

/**
 * 检索页的跨页面 UI 状态：勾了哪些库 + 选了哪把 Key。
 *
 * 为什么进 Pinia 而不是页面 `ref`：勾选与选 Key 都是「去建库 / 看列表 / 改配置再回来」
 * 时不该丢的东西，页面组件会随路由重建，只有应用级的 store 能活过这次重建。
 *
 * 本票（#92）在这里加了 `selectedKeyId` 与「这把 Key 的原文」：
 *
 * - 原文**只进内存**（本 store 的 ref + `lib/activeKey` 的模块变量），不落
 *   `localStorage` / `sessionStorage`，也不进 URL。刷新页面即失效——这是有意的；
 * - 原文与 id 由 :func:`selectKey` **一次性**写入 `lib/activeKey`：请求层认的 Key
 *   与页面显示的 Key 因此只有一条同步路径，不会出现「页面换了、请求还带旧 Key」；
 * - 选中一把 Key 同时确定了 `allowedKbIds`，而**换 Key 必须把不在新白名单里的已勾库
 *   移除**（并把被移除的 id 交回页面去提示）。留着它们会让下一次检索撞 20008，
 *   而错误要等请求之后才出现，用户看不到原因。空白名单表示「什么都没圈定」，
 *   不是「全部」——所以它也把已勾库全部移除。
 *
 * 也不落 storage：库 ID 本身不是凭据，但把它持久化会掩盖「换 Key 后白名单变了」这类
 * 真实问题——页面重新读一次可见库清单才更可信。
 */
export interface SelectKeyInput {
  keyId: string
  /** Key 原文（`GET /console/keys` 的 `key_value`）。 */
  keyValue: string
  /** 这把 Key 的配置白名单；配置缺失时传空列表。 */
  allowedKbIds: string[]
}

export const useRetrieveStore = defineStore('retrieve', () => {
  const selectedKbIds = ref<string[]>([])
  const selectedKeyId = ref<string | null>(null)
  /** 当前 Key 的 `allowed_kb_ids`；空列表 = 什么都没圈定。 */
  const allowedKbIds = ref<string[]>([])

  const selectedCount = computed(() => selectedKbIds.value.length)

  function isSelected(kbId: string): boolean {
    return selectedKbIds.value.includes(kbId)
  }

  function isAllowed(kbId: string): boolean {
    return allowedKbIds.value.includes(kbId)
  }

  /**
   * 整体替换选择（勾选框组的更新走这里）。
   *
   * 去重是必要的：同一个 `kb_id` 从 URL 预填进来、又在清单里被勾一次，就会出现两条，
   * 而 `kb_ids` 里放重复 ID 会让「已选 N / 共 M」和请求体都看起来不对劲。
   */
  function setSelection(kbIds: string[]): void {
    selectedKbIds.value = [...new Set(kbIds)]
  }

  /**
   * `?kb_id=` 预填一个库。
   *
   * 是「加进选择」而不是「只剩它」：从列表页带着某个库跳过来时，用户此前勾好的其它库
   * 不该被一次跳转静默清空；空值（没带 query）不产生任何选择。
   */
  function prefill(kbId: string): void {
    const trimmed = kbId.trim()
    if (trimmed === '' || selectedKbIds.value.includes(trimmed)) return
    selectedKbIds.value = [...selectedKbIds.value, trimmed]
  }

  /**
   * 选中 / 切换 Key。
   *
   * 先写请求层、再裁选择：任何返回 `removed` 的调用方都能立刻把这些 id 拿去提示用户
   * 「这些库已经被移出选择」；`removed` 为空表示这次只是确认或重选同一把 Key。
   */
  function selectKey(input: SelectKeyInput): string[] {
    selectedKeyId.value = input.keyId
    allowedKbIds.value = [...new Set(input.allowedKbIds)]
    setActiveKey(input.keyValue)

    const allowed = new Set(allowedKbIds.value)
    const removed = selectedKbIds.value.filter((kbId) => !allowed.has(kbId))
    if (removed.length > 0) {
      selectedKbIds.value = selectedKbIds.value.filter((kbId) => allowed.has(kbId))
    }
    return removed
  }

  /** 清空选择：请求层回到「还没选」，页面下次检索会拿到可操作的报错。 */
  function clearKey(): void {
    selectedKeyId.value = null
    allowedKbIds.value = []
    setActiveKey(null)
  }

  function clear(): void {
    selectedKbIds.value = []
  }

  return {
    selectedKbIds,
    selectedKeyId,
    allowedKbIds,
    selectedCount,
    isSelected,
    isAllowed,
    setSelection,
    prefill,
    selectKey,
    clearKey,
    clear,
  }
})
