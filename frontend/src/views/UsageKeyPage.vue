<script setup lang="ts">
/**
 * 单把 Key 的用量（第 14 步 #203）。
 *
 * 与租户页同一套指标，只收窄到一把 Key——**不重写一套口径**：同一个接口带 `key_id`，
 * 后端先校验它属于本租户（不属于就 `20011`），所以这一页拿到的数一定是自己的。
 *
 * 为什么要单独一页而不是弹窗：单 Key 的问题是「它最近是不是在拖」，
 * 那需要时间轴；弹窗里画一条轴，宽度和高度都不够。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import PageHeader from '../components/PageHeader.vue'
import UsageChart from '../components/UsageChart.vue'
import { fetchTenantUsage, usageQueryKey } from '../services/usageService'
import { USAGE_WINDOWS, type UsageWindow } from '../types/usage'
import { errorText } from '../utils/errorText'
import { formatCount, formatMs, formatRatio, p95Label, sourceLabel } from '../utils/usageFormat'

const route = useRoute()
const keyId = computed(() => String(route.params.keyId ?? ''))
const window = ref<UsageWindow>('1d')

const query = useQuery({
  queryKey: computed(() => usageQueryKey('key', window.value, keyId.value)),
  queryFn: () => fetchTenantUsage(window.value, keyId.value),
})

const data = computed(() => query.data.value ?? null)
const loadError = computed(() => (query.isError.value ? errorText(query.error.value) : ''))
const totals = computed(() => data.value?.totals ?? null)
</script>

<template>
  <div class="space-y-6">
    <PageHeader title="单把 Key" :subtitle="keyId === '' ? '缺少 key_id' : keyId">
      <template #actions>
        <el-radio-group v-model="window" size="small">
          <el-radio-button v-for="item in USAGE_WINDOWS" :key="item.value" :value="item.value">
            {{ item.label }}
          </el-radio-button>
        </el-radio-group>
      </template>
    </PageHeader>

    <p v-if="loadError !== ''" class="card card-body text-sm text-red-700">{{ loadError }}</p>

    <template v-else>
      <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div class="card card-body">
          <p class="metric-label">调用次数</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatCount(totals?.requests) }}</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">成功率</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatRatio(totals?.successRate) }}</p>
          <p v-if="(totals?.unknownCode ?? 0) > 0" class="mt-1 text-xs text-muted">
            其中 {{ formatCount(totals?.unknownCode) }} 次未统计
          </p>
        </div>
        <div class="card card-body">
          <p class="metric-label">P50 耗时</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatMs(totals?.p50Ms) }}</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">{{ p95Label(data?.p95Source ?? 'exact') }}</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatMs(totals?.p95Ms) }}</p>
        </div>
      </div>

      <section class="card card-body">
        <div class="flex items-baseline justify-between gap-2">
          <h2 class="section-title">三本账</h2>
          <span class="text-xs text-muted">{{ sourceLabel(data?.source ?? 'events') }}</span>
        </div>
        <div v-if="(data?.buckets.length ?? 0) === 0" class="py-10 text-center text-sm text-muted">
          这个窗口内这把 Key 没有调用。
        </div>
        <UsageChart v-else :buckets="data?.buckets ?? []" kind="stacked" :height="220" />
      </section>

      <section class="card card-body">
        <h2 class="section-title">成功率与 P95</h2>
        <UsageChart
          v-if="(data?.buckets.length ?? 0) > 0"
          :buckets="data?.buckets ?? []"
          kind="quality"
          :height="180"
        />
      </section>
    </template>
  </div>
</template>
