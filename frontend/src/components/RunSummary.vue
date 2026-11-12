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

    <dl class="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
      <div v-for="row in rows" :key="row.field">
        <dt class="text-xs text-slate-400">
          {{ row.label }}
          <code class="ml-1 rounded bg-slate-100 px-1 font-mono text-xs text-slate-500">
            {{ row.field }}
          </code>
        </dt>
        <dd class="mt-0.5 break-all font-mono text-slate-800">
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
      <code>tenant_policy</code> 是「本次实际生效」的唯一真相：档位想要的重排 / 改写
      可能被部署能力或套餐上限裁掉，以上面显示的值为准。
    </p>
  </div>
</template>
