<script setup lang="ts">
/**
 * 运行摘要（只读）：把一次响应的 `metadata` 摊成字段清单。
 *
 * 抽成独立组件的两个理由：
 *
 * 1. **它是纯展示**：只收 `metadata` 一个 prop，不读 store、不发请求、也拿不到页面上的
 *    任何输入（Key、勾选的库、问题框）。「摘要只回答后端这次实际生效了什么」这条约定
 *    因此从代码结构上就成立，而不是靠注释提醒；
 * 2. **它需要渲染测**：spec 的 Testing Decisions 要求「检索摘要的组件测（mock 网络）」，
 *    纯函数测只能钉住 `buildRunSummary` 的输出，钉不住「英文码照原文显示」「null 显示
 *    『—』」这些只有模板才决定的事。
 *
 * 英文码（mode / vector_store / degraded_reason / empty_reason）一律照原文显示，中文只
 * 进 tooltip：拿中文替身会让人拿着翻译去搜日志却搜不到。
 */
import { computed } from 'vue'

import type { RetrievalMetadata } from '../types/retrieval'
import { buildRunSummary } from '../utils/format'

const props = defineProps<{
  /** 这一次响应的 `metadata`；父组件保证它不是 null（没响应时不渲染本组件）。 */
  metadata: RetrievalMetadata
}>()

const rows = computed(() => buildRunSummary(props.metadata))
const degraded = computed(() => props.metadata.retrieval.degraded)
const degradedReason = computed(() => props.metadata.retrieval.degraded_reason)
const hasTenantPolicy = computed(() => props.metadata.tenant_policy !== undefined)
</script>

<template>
  <div>
    <div
      v-if="degraded"
      class="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
    >
      本次结果不完整（degraded = true）：{{ degradedReason ?? '后端没有给出原因' }}。
      降级那一路的分数、名次会是「—」。
    </div>

    <!--
      **一行一项**，不是多列网格。原来用的是 `lg:grid-cols-3`：这一栏在最窄的右列里只有
      ~340px，三列把每格压到 100px 出头，而 `tenant_policy.clipped` 这种字段名不换行，
      于是直接溢到隔壁格子里，标签和值叠在一起互相盖住（真实截图就是这样）。
      行布局不依赖容器宽度：标签在左、值在右，放不下就换行，永远不会越界。
      列宽不再参与决定可读性，这段就再也不会因为「换个屏幕宽度」而崩。
    -->
    <dl class="text-sm">
      <div
        v-for="row in rows"
        :key="row.field"
        class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border-b border-line/70 py-1.5 last:border-b-0"
      >
        <dt class="min-w-0 break-all text-xs text-slate-400">
          {{ row.label }}
          <code class="ml-1 rounded bg-slate-100 px-1 font-mono text-[11px] text-slate-500">
            {{ row.field }}
          </code>
        </dt>
        <dd class="ml-auto min-w-0 break-all text-right font-mono text-slate-800">
          <el-tooltip v-if="row.hint !== null" :content="row.hint" placement="top">
            <span class="cursor-help underline decoration-slate-300 decoration-dotted">
              {{ row.value }}
            </span>
          </el-tooltip>
          <span v-else>{{ row.value }}</span>
        </dd>
      </div>
    </dl>

    <p v-if="!hasTenantPolicy" class="mt-3 text-xs text-slate-400">
      这次响应没有 <code>tenant_policy</code>：只有经套餐门禁解析的调用才有这一栏。
    </p>
    <p v-else class="mt-3 text-xs text-slate-400">
      <code>tenant_policy</code> 是本次实际生效的值（重排 / 改写可能被能力或上限裁掉）。
    </p>
  </div>
</template>
