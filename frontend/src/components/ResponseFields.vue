<script setup lang="ts">
/**
 * 一棵**完整字段树**：把任意响应值逐层摊开，一个字段都不漏。
 *
 * 为什么不用「按接口写死一张字段表」：四个检索端点的 `metadata` 各带各的分组
 * （`retrieval.rerank` / `retrieval.query_optimization` / `retrieval.decomposition`，
 * Research 又是 `plan` / `rounds` / `coverage` …），写死的表**天然会漏**——新加一个字段，
 * 表格里就没有它，而调用方恰恰是靠这些字段判断「后端这次到底做了什么」的。
 * 递归渲染的代价是版面不如手工排版漂亮，换来的是「后端加字段，这里立刻显示」。
 *
 * 三条显示口径，与「运行摘要」一致：
 * * 英文码 / 布尔 / 数字**原样**输出，不翻译、不美化（翻译过就搜不到了）；
 * * `null` 显示成 `null` 而不是「—」：这一层要的是「字段存在且为空」，不是「没有这个字段」；
 * * 容器为空时显示 `{}` / `[]`，不省略——「是空数组」和「没这个字段」是两件事。
 */
import { computed } from 'vue'

const props = defineProps<{
  /** 任意 JSON 值（响应里的一个节点）。 */
  value: unknown
  /** 缩进层次；由父级递归传入，根节点是 0。 */
  depth?: number
}>()

const depth = computed(() => props.depth ?? 0)

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

const entries = computed<[string, unknown][]>(() =>
  kind.value === 'object' ? Object.entries(props.value as Record<string, unknown>) : [],
)

const items = computed<unknown[]>(() =>
  kind.value === 'array' ? (props.value as unknown[]) : [],
)

const emptyText = computed<string>(() => (kind.value === 'empty-array' ? '[]' : '{}'))

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

/** 子层的左缩进：一层 12px，够辨认层次又不至于把长文本挤成窄条。 */
const childIndent = computed(() => ({ marginLeft: `${(depth.value + 1) * 12}px` }))
</script>

<template>
  <div>
    <span v-if="kind === 'null'" class="text-slate-400">null</span>

    <span
      v-else-if="kind === 'empty-object' || kind === 'empty-array'"
      class="text-slate-400"
    >
      {{ emptyText }}
    </span>

    <span v-else-if="kind === 'scalar'" :class="scalarTone" class="break-all">
      {{ scalarText }}
    </span>

    <!-- 对象：键一行、子块缩进一层。键与值分开才不会让长值把键挤没。 -->
    <template v-else-if="kind === 'object'">
      <div v-for="[key, child] in entries" :key="key" class="mt-0.5">
        <span class="text-slate-500">{{ key }}</span>
        <span class="text-slate-400">:</span>
        <span
          v-if="
            child === null ||
            typeof child !== 'object' ||
            (Array.isArray(child) && child.length === 0) ||
            (!Array.isArray(child) && Object.keys(child as object).length === 0)
          "
          class="ml-1"
        >
          <ResponseFields :value="child" :depth="depth + 1" />
        </span>
        <div v-else :style="childIndent">
          <ResponseFields :value="child" :depth="depth + 1" />
        </div>
      </div>
    </template>

    <!-- 数组：下标一行、元素缩进一层。 -->
    <template v-else>
      <div v-for="(item, index) in items" :key="index" class="mt-0.5">
        <span class="text-slate-400">[{{ index }}]</span>
        <span
          v-if="
            item === null ||
            typeof item !== 'object' ||
            (Array.isArray(item) && item.length === 0) ||
            (!Array.isArray(item) && Object.keys(item as object).length === 0)
          "
          class="ml-1"
        >
          <ResponseFields :value="item" :depth="depth + 1" />
        </span>
        <div v-else :style="childIndent">
          <ResponseFields :value="item" :depth="depth + 1" />
        </div>
      </div>
    </template>
  </div>
</template>
