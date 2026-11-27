import { useQuery } from '@tanstack/vue-query'
import { computed, ref, watch, type Ref } from 'vue'

import {
  documentsRefreshMode,
  intervalOf,
  listRefreshMode,
  type RefreshMode,
} from '../lib/refreshPolicy'
import { fetchKbDocuments, fetchKbList, KB_DOCUMENTS_QUERY_KEY, KB_LIST_QUERY_KEY } from '../services/consoleKb'
import type { KbDocumentsQuery } from '../types/knowledgeBase'

/**
 * 知识库页的两个查询：**列表**（卡片）与**详情**（一个库的一页文档），各自一条刷新节奏。
 *
 * ## 为什么是两个查询而不是一个
 *
 * 以前是一个树查询同时服务卡片与详情弹窗，于是：卡片只需要四个数字，却被一个「整个租户的
 * 全部文档」的响应喂着；详情没有自己的请求，只能读列表上一次轮询的快照——这就是「进详情
 * 总要刷一次才看到真状态」的来源。拆成两条之后，两条各自回答自己的问题。
 *
 * ## 为什么判定逻辑都不在这里
 *
 * 「什么时候快刷」是纯函数（`lib/refreshPolicy.ts`），这里只负责把它的结果塞进
 * `refetchInterval`。判定留在 composable 里就没法单测——它要么依赖定时器，要么依赖组件
 * 生命周期，而这类 bug（「停了但看不出来」）恰好只在那两处显形。
 */

/** 列表查询：卡片网格。 */
export function useKbListQuery() {
  const query = useQuery({
    queryKey: KB_LIST_QUERY_KEY,
    queryFn: fetchKbList,
    refetchInterval: (q) => intervalOf(listRefreshMode(q.state.data)),
    // 后台也要跑：慢查那一档的职责是「别人动过这个库」，而别人的动作不分你的窗口在不在前台。
    // 默认 false 会让「切到另一个标签页」时心跳整段不发——那正是验收第 6 条要的场景。
    refetchIntervalInBackground: true,
    // 回前台立刻核对一次：焦点是最便宜、也最准确的「我可能错过了什么」信号。
    refetchOnWindowFocus: true,
  })

  const items = computed(() => query.data.value?.items ?? [])
  const mode = computed<RefreshMode>(() => listRefreshMode(query.data.value))
  const updatedAt = computed(() => query.data.value?.generated_at ?? null)

  return { query, items, mode, updatedAt }
}

/** 详情页的筛选（页码、每页条数、状态、标题关键字）。 */
export interface KbDocumentsFilter {
  page: Ref<number>
  pageSize: Ref<number>
  status: Ref<number | undefined>
  keyword: Ref<string>
}

/** 关键字要防抖多久。300ms 是「停下来就已经出结果」的经验值。 */
export const KEYWORD_DEBOUNCE_MS = 300

/** 每页条数的可选项（`el-pagination` 的 `page-sizes`）。 */
export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const

/**
 * 把一份筛选状态翻成请求参数。**纯函数**，所以「页码越界」「空关键字该不该传」这类
 * 边界能直接钉住，不用挂组件。
 *
 * 空关键字传 `undefined`（不是 `''`）：后端 `keyword=""` 会被当成一个筛选条件，而
 * 「没搜」与「搜空串」在分页上不该有任何差别。
 */
export function documentsQueryParams(filter: {
  page: number
  pageSize: number
  status: number | undefined
  keyword: string
}): KbDocumentsQuery {
  const keyword = filter.keyword.trim()
  return {
    page: Math.max(filter.page, 1),
    pageSize: filter.pageSize,
    status: filter.status,
    keyword: keyword === '' ? undefined : keyword,
  }
}

/**
 * 详情查询的缓存键：**纯数组、全基础值**。
 *
 * 不用 `reactive({...})` 包一层参数对象：query key 的相等性靠结构比对，里面塞一个响应式代理
 * 会让「翻回上一页」变成一次新请求（每次渲染都生成一个新代理），也与其他带筛选的查询
 * （`usageQueryKey` 等）的写法不一致。全基础值的数组没有这个问题。
 */
export function documentsQueryKey(kbId: string | null, query: KbDocumentsQuery): readonly unknown[] {
  return [
    ...KB_DOCUMENTS_QUERY_KEY,
    kbId,
    query.page,
    query.pageSize,
    query.status ?? null,
    query.keyword ?? '',
  ]
}

/**
 * 一个最小的防抖器（可注入定时器用于单测）。
 *
 * 为什么单独抽出来：`setTimeout` + `clearTimeout` 的那两行是**有状态**的（上一个 timer 忘了
 * 清，输入就会在一次停顿之后连发两个请求）。留在 composable 里只能靠 `?raw` 源码串断言
 * 「常量名存在」，那什么都没证明；抽出来之后用假定时器就能钉住「只发一次、发最后一次」。
 */
export interface DebouncerTimers {
  /** 排定一次回调；返回值原样交回给 `cancel`。 */
  schedule: (fn: () => void, ms: number) => unknown
  /** 取消一次已排定的回调（拿 `schedule` 的返回值）。 */
  cancel: (handle: unknown) => void
}

/** 默认定时器：浏览器/Node 的 `setTimeout` / `clearTimeout`。 */
export const REAL_TIMERS: DebouncerTimers = {
  schedule: (fn, ms) => setTimeout(fn, ms),
  cancel: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
}

export function createDebouncer(
  delayMs: number,
  timers: DebouncerTimers = REAL_TIMERS,
): { push: (fn: () => void) => void } {
  let handle: unknown = null
  return {
    push(fn: () => void): void {
      if (handle !== null) timers.cancel(handle)
      handle = timers.schedule(() => {
        handle = null
        fn()
      }, delayMs)
    },
  }
}

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
  const debouncer = createDebouncer(KEYWORD_DEBOUNCE_MS)
  const debouncedKeyword = ref('')
  watch(filter.keyword, (value) => {
    debouncer.push(() => {
      debouncedKeyword.value = value.trim()
    })
  })

  // 筛选条件变了就回第一页：停在第 5 页看新的筛选结果，多半是空页。
  watch([filter.status, debouncedKeyword], () => {
    filter.page.value = 1
  })

  const params = computed(() =>
    documentsQueryParams({
      page: filter.page.value,
      pageSize: filter.pageSize.value,
      status: filter.status.value,
      keyword: debouncedKeyword.value,
    }),
  )

  const query = useQuery({
    queryKey: computed(() => documentsQueryKey(kbId.value, params.value)),
    queryFn: () => {
      const id = kbId.value
      if (id === null) {
        // `enabled` 已经挡住这条路径；真走到这里说明状态机错了，说出来比静默发一个 null 好。
        throw new Error('详情查询需要 kb_id，而它此刻是 null')
      }
      return fetchKbDocuments(id, params.value)
    },
    enabled: computed(() => kbId.value !== null),
    refetchInterval: (q) => intervalOf(documentsRefreshMode(q.state.data)),
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  })

  const payload = computed(() => query.data.value ?? null)
  const mode = computed<RefreshMode>(() => documentsRefreshMode(query.data.value))
  const updatedAt = computed(() => query.data.value?.generated_at ?? null)

  // 只回页面真正用得到的东西：`debouncedKeyword` 是内部状态（页面不该看到一个"半成品关键字"）。
  return { query, payload, mode, updatedAt }
}
