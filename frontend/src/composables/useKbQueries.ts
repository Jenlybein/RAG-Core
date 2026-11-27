import { useQuery } from '@tanstack/vue-query'
import { computed, reactive, ref, watch, type ComputedRef, type Ref } from 'vue'

import {
  intervalOf,
  documentsRefreshMode,
  listRefreshMode,
  type RefreshMode,
} from '../lib/refreshPolicy'
import {
  fetchKbDocuments,
  fetchKbList,
  KB_DOCUMENTS_QUERY_KEY,
  KB_LIST_QUERY_KEY,
} from '../services/consoleKb'
import type { KbDocumentsPayload, KbListPayload, KbDocumentsQuery } from '../types/knowledgeBase'

/**
 * 知识库页的两个查询：**列表**（卡片）与**详情**（一个库的一页文档），各自一条刷新节奏。
 *
 * ## 为什么是两个查询而不是一个
 *
 * 以前是一个树查询同时服务卡片与详情弹窗，于是：卡片只需要四个数字，却被一个「整个租户的
 * 全部文档」的响应喂着；详情没有自己的请求，只能读列表上一次轮询的快照——这就是「进详情
 * 总要刷一次才看到真状态」的来源。拆成两条之后，两条各自回答自己的问题：
 *
 * * 列表：哪些库、各多少篇、有没有在跑的构建；
 * * 详情：**这个库**这一页文档，以及它们与当前这一版构建的关系。
 *
 * ## 刷新为什么写在 composable 里
 *
 * `refetchInterval` 的回调要读「当前这份数据」，交给回调自带的 query 对象读最新值，才不会
 * 在每次刷新时读到上一次的闭包。这一条是上一版（`useTreePolling`）踩实的经验，照搬。
 */

/** 列表查询：卡片网格。 */
export function useKbListQuery() {
  const query = useQuery({
    queryKey: KB_LIST_QUERY_KEY,
    queryFn: fetchKbList,
    // 永远返回一个间隔：空闲时是 60 秒慢查，不是「彻底停止」。
    // 停下来的界面没法自证「我停在哪一刻」，那正是要修的东西（见 lib/refreshPolicy.ts）。
    refetchInterval: (q) => intervalOf(listRefreshMode(q.state.data as KbListPayload | undefined)),
  })

  const items = computed(() => query.data.value?.items ?? [])
  const mode = computed<RefreshMode>(() => listRefreshMode(query.data.value))
  const updatedAt = computed(() => query.data.value?.generated_at ?? null)

  return { query, items, mode, updatedAt }
}

/** 详情页的取数参数（页码、每页条数、状态筛选、标题关键字）。 */
export interface KbDocumentsFilter {
  page: Ref<number>
  pageSize: Ref<number>
  status: Ref<number | undefined>
  keyword: Ref<string>
}

/**
 * 关键字按**服务端**筛，所以要防抖：每敲一个字发一次请求，既浪费也让人看到跳动。
 *
 * 300ms 是「停下来就已经出结果」的经验值；与分页参数分开是因为它单独变化时不该重置页码。
 */
const KEYWORD_DEBOUNCE_MS = 300

export function useKbDocumentsFilter(): KbDocumentsFilter {
  return {
    page: ref(1),
    pageSize: ref(50),
    status: ref<number | undefined>(undefined),
    keyword: ref(''),
  }
}

/** 详情查询：只在弹窗打开（`kbId` 非空）时启用，打开即取。 */
export function useKbDocumentsQuery(kbId: Ref<string | null>, filter: KbDocumentsFilter) {
  const debouncedKeyword = ref('')
  let timer: ReturnType<typeof setTimeout> | null = null
  watch(
    filter.keyword,
    (value) => {
      if (timer !== null) clearTimeout(timer)
      timer = setTimeout(() => {
        debouncedKeyword.value = value.trim()
      }, KEYWORD_DEBOUNCE_MS)
    },
    { flush: 'post' },
  )

  // 筛选条件变了就回第一页：停在第 5 页看新的筛选结果，多半是空页。
  watch([filter.status, debouncedKeyword], () => {
    filter.page.value = 1
  })

  const params = computed<KbDocumentsQuery>(() => ({
    page: filter.page.value,
    pageSize: filter.pageSize.value,
    status: filter.status.value,
    keyword: debouncedKeyword.value === '' ? undefined : debouncedKeyword.value,
  }))

  const query = useQuery({
    // 键里带筛选条件：不同页/不同筛选各有一份缓存，翻回去不用重取。
    queryKey: computed(() => [...KB_DOCUMENTS_QUERY_KEY, kbId.value, reactive({ ...params.value })]),
    queryFn: () =>
      fetchKbDocuments(kbId.value as string, params.value),
    enabled: computed(() => kbId.value !== null),
    refetchInterval: (q) =>
      intervalOf(documentsRefreshMode(q.state.data as KbDocumentsPayload | undefined)),
  })

  const payload = computed(() => query.data.value ?? null)
  const mode = computed<RefreshMode>(() => documentsRefreshMode(query.data.value))
  const updatedAt = computed(() => query.data.value?.generated_at ?? null)

  return { query, payload, mode, updatedAt, debouncedKeyword }
}

/** 供外部（表格分页器）读的每页条数选项。 */
export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

/** 详情页的行类型别名（模板里用得上）。 */
export type { KbDocumentsPayload, KbListPayload }

/** 类型收口：`ComputedRef` 在模板里可以直接读，这里显式导出一次，避免模板里出现裸 `Ref`。 */
export type KbItems = ComputedRef<ReturnType<typeof useKbListQuery>['items']['value']>
