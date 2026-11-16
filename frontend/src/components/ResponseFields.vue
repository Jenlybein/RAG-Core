<script setup lang="ts">
/**
 * 可折叠的 JSON 字段树：把任意响应值逐层摊开，一层一层点开看。
 *
 * 为什么不用「按接口写死一张字段表」：四个检索端点的 `metadata` 各带各的分组
 * （`retrieval.rerank` / `retrieval.query_optimization` / `retrieval.decomposition`，
 * Research 又是 `plan` / `rounds` / `coverage` …），写死的表**天然会漏**——新加一个字段，
 * 表格里就没有它，而调用方恰恰是靠这些字段判断「后端这次到底做了什么」的。
 * 递归渲染的代价是版面不如手工排版漂亮，换来的是「后端加字段，这里立刻显示」。
 *
 * 为什么是**折叠**而不是全部摊开：`/rewrite` 一次能回 50 条切片、`decomposition` 能拆
 * 五六条子问题，整棵树摊开会把这一栏撑成几屏，什么都看不出来。折叠后一行能读出
 * 「这里有几个键」，要细节时点开那一层。
 *
 * 三条显示口径与「运行摘要」一致：
 * * 英文码 / 布尔 / 数字**原样**输出，不翻译、不美化（翻译过就搜不到了）；
 * * `null` 显示成 `null` 而不是「—」：这一层要的是「字段存在且为空」，不是「没有这个字段」；
 * * 容器为空时显示 `{}` / `[]`，不省略——「是空数组」和「没这个字段」是两件事。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  /** 任意 JSON 值（响应里的一个节点）。 */
  value: unknown
  /** 这个节点在父级里的键名或下标；根节点没有。 */
  label?: string | null
  /** 缩进层次；由父级递归传入，根节点是 0。 */
  depth?: number
  /** 小于这一层的容器默认展开；到达这层及更深的默认收起。 */
  defaultOpenDepth?: number
  /**
   * 父级广播的「全部展开 / 全部收起」。`null` 表示还没广播过（跟随 `defaultOpenDepth`）。
   *
   * 用 `'open' / 'closed'` 两个词而不是布尔：Vue 会把**布尔** prop 在「没传」时转成
   * `false`，于是「没传」与「要求全部收起」分不开——一开始每个节点都会以为自己该收起。
   * 字符串没有这个转换，缺席就是缺席。
   */
  expandAll?: 'open' | 'closed' | null
  /** 广播序号。值一变，本节点就把展开状态对齐到 `expandAll`。 */
  serial?: number
}>()

const depth = computed(() => props.depth ?? 0)
const openDepth = computed(() => props.defaultOpenDepth ?? 1)
/** 根不折叠：收起后整张卡就是空白，那个点击没有意义。 */
const isRoot = computed(() => depth.value === 0 && (props.label ?? null) === null)

type NodeKind = 'null' | 'scalar' | 'empty-object' | 'empty-array' | 'object' | 'array'

const kind = computed<NodeKind>(() => {
  const value = props.value
  if (value === null || value === undefined) return 'null'
  if (Array.isArray(value)) return value.length === 0 ? 'empty-array' : 'array'
  if (typeof value === 'object') {
    return Object.keys(value as Record<string, unknown>).length === 0 ? 'empty-object' : 'object'
  }
  return 'scalar'
})

const isContainer = computed<boolean>(
  () => kind.value === 'object' || kind.value === 'array',
)

const entries = computed<[string, unknown][]>(() =>
  kind.value === 'object' ? Object.entries(props.value as Record<string, unknown>) : [],
)

const items = computed<unknown[]>(() =>
  kind.value === 'array' ? (props.value as unknown[]) : [],
)

const emptyText = computed<string>(() => (kind.value === 'empty-array' ? '[]' : '{}'))

/** 收起时的那一行摘要：先告诉人「这里有多少项」，再决定要不要点开。 */
const summary = computed<string>(() => {
  if (kind.value === 'object') return `{${entries.value.length} 项}`
  if (kind.value === 'array') return `[${items.value.length} 项]`
  return ''
})

/**
 * 初始展开状态。
 *
 * 父级的「全部展开 / 全部收起」**优先于**默认深度：广播发生时还没挂载的深层节点，
 * 是在父节点展开之后才被渲染的——它们初始化时若只读 `defaultOpenDepth`，就会在
 * 「全部展开」之后仍然是收起的（漏一层就等于这个按钮没实现）。所以先把 `expandAll`
 * 当作初始值，没有广播过才退回默认深度。
 */
function initialOpen(): boolean {
  // 根不折叠：收起后整张卡就是空白，那个点击没有意义。
  if (isRoot.value && isContainer.value) return true
  if (props.expandAll === 'open') return true
  if (props.expandAll === 'closed') return false
  return depth.value < openDepth.value
}

const open = ref(initialOpen())

// 对齐父级的「全部展开 / 全部收起」。根不参与：它没有折叠按钮。
watch(
  () => props.serial,
  () => {
    if (isRoot.value || props.expandAll === null || props.expandAll === undefined) return
    open.value = props.expandAll === 'open'
  },
)

function toggle(): void {
  open.value = !open.value
}

/** 标量原文。空串显示成 `""`：否则它看起来像「这个字段没有值」。 */
const scalarText = computed<string>(() => {
  const value = props.value
  if (typeof value === 'string') return value === '' ? '""' : value
  return String(value)
})

const scalarTone = computed<string>(() => {
  const value = props.value
  if (typeof value === 'number') return 'text-ink'
  if (typeof value === 'boolean') return 'text-brand'
  return 'text-slate-700'
})

/** 子层缩进：一层 12px，够辨认层次又不至于把长文本挤成窄条。 */
const childIndent = computed(() => ({ marginLeft: `${(depth.value + 1) * 12}px` }))
</script>

<template>
  <div>
    <!-- 标量：一行 key: value，不需要折叠。 -->
    <div v-if="kind === 'scalar' || kind === 'null' || kind === 'empty-object' || kind === 'empty-array'">
      <span v-if="label !== undefined && label !== null" class="text-slate-500">{{ label }}</span>
      <span v-if="label !== undefined && label !== null" class="text-slate-400">: </span>
      <span v-if="kind === 'null'" class="text-slate-400">null</span>
      <span
        v-else-if="kind === 'empty-object' || kind === 'empty-array'"
        class="text-slate-400"
      >
        {{ emptyText }}
      </span>
      <span v-else :class="scalarTone" class="break-all">{{ scalarText }}</span>
    </div>

    <!-- 容器：一行「键 + 项数」的折叠按钮，点头部才展开。 -->
    <div v-else>
      <button
        type="button"
        class="group inline-flex max-w-full flex-wrap items-baseline gap-1 text-left"
        @click="toggle"
      >
        <span class="w-3 shrink-0 text-[10px] text-slate-400">{{ open ? '▼' : '▶' }}</span>
        <span v-if="label !== undefined && label !== null" class="text-slate-500">
          {{ label }}
        </span>
        <span class="text-slate-400">{{ summary }}</span>
      </button>

      <div v-if="open" :style="childIndent">
        <template v-if="kind === 'object'">
          <ResponseFields
            v-for="[key, child] in entries"
            :key="key"
            :value="child"
            :label="key"
            :depth="depth + 1"
            :default-open-depth="openDepth"
            :expand-all="expandAll ?? null"
            :serial="serial ?? 0"
          />
        </template>
        <template v-else>
          <ResponseFields
            v-for="(item, index) in items"
            :key="index"
            :value="item"
            :label="`[${index}]`"
            :depth="depth + 1"
            :default-open-depth="openDepth"
            :expand-all="expandAll ?? null"
            :serial="serial ?? 0"
          />
        </template>
      </div>
    </div>
  </div>
</template>
