import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

/**
 * 检索页的跨页面 UI 状态：**只存「勾了哪些库」**。
 *
 * 为什么进 Pinia 而不是页面 `ref`：勾选是「去建库 / 看列表再回来」时不该丢的东西，
 * 页面组件会随路由重建，只有应用级的 store 能活过这次重建。
 *
 * 刻意**没有** `selectedKeyId`：选哪把 Key 是 #92 的票（它还要先把 Key 列表接口做出来），
 * 本票的 Key 来自构建期环境变量。把字段先埋进来只会让「当前用的是哪把 Key」出现两个
 * 可能的答案（构建期那个 vs store 里那个）。
 *
 * 也不落 storage：库 ID 本身不是凭据，但把它持久化会掩盖「换 Key 后白名单变了」这类
 * 真实问题——页面重新读一次可见库清单才更可信。
 */
export const useRetrieveStore = defineStore('retrieve', () => {
  const selectedKbIds = ref<string[]>([])
  const selectedCount = computed(() => selectedKbIds.value.length)

  function isSelected(kbId: string): boolean {
    return selectedKbIds.value.includes(kbId)
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

  function clear(): void {
    selectedKbIds.value = []
  }

  return { selectedKbIds, selectedCount, isSelected, setSelection, prefill, clear }
})
