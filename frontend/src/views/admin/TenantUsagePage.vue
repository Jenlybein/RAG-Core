<script setup lang="ts">
/**
 * 管理员下钻到某个租户（第 14 步 #203）。
 *
 * 与「用量」页的租户口径**同一份实现**（同一个服务层、同一套指标），区别只有两个：
 * 它跨租户（管理员身份），以及页头写明这里看的是别人的账。
 *
 * 查一个不存在的租户得到全 0 的空图而不是 404：那回答了「这个 id 到底有没有量」，
 * 比一句「找不到」有用。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import PageHeader from '../../components/PageHeader.vue'
import UsageChart from '../../components/UsageChart.vue'
import { fetchTenantUsageDetail, usageQueryKey } from '../../services/usageService'
import { USAGE_WINDOWS, type UsageWindow } from '../../types/usage'
import { errorText } from '../../utils/errorText'
import { formatCount, formatMs, formatRatio, p95Label, sourceLabel } from '../../utils/usageFormat'

const route = useRoute()
const tenantId = computed(() => String(route.params.tenantId ?? ''))
const window = ref<UsageWindow>('1d')

const query = useQuery({
  queryKey: computed(() => usageQueryKey('tenant-detail', window.value, tenantId.value)),
  queryFn: () => fetchTenantUsageDetail(tenantId.value, window.value),
})

const data = computed(() => query.data.value ?? null)
const loadError = computed(() => (query.isError.value ? errorText(query.error.value) : ''))
const totals = computed(() => data.value?.totals ?? null)
</script>

<template>
  <div class="space-y-6">
    <PageHeader :title="tenantId === '' ? '租户用量' : tenantId" subtitle="这个租户的调用、成功率与它的 Key。">
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
          <h2 class="section-title">趋势</h2>
          <span class="text-xs text-muted">{{ sourceLabel(data?.source ?? 'events') }}</span>
        </div>
        <div v-if="(data?.buckets.length ?? 0) === 0" class="py-10 text-center text-sm text-muted">
          这个窗口内该租户没有调用记录。
        </div>
        <UsageChart v-else :buckets="data?.buckets ?? []" kind="stacked" :height="220" />
      </section>

      <section class="card">
        <div class="card-body flex items-baseline justify-between gap-2">
          <h2 class="section-title">它的 Key</h2>
          <span class="text-xs text-muted">已删除的 Key 保留历史</span>
        </div>
        <el-table :data="data?.keys ?? []" size="small" empty-text="这个窗口内没有 Key 调用过">
          <el-table-column label="Key" min-width="180">
            <template #default="{ row }">
              <span class="font-medium">{{ row.label ?? '（未命名）' }}</span>
              <span v-if="row.masked" class="ml-2 text-xs text-muted">{{ row.masked }}</span>
            </template>
          </el-table-column>
          <el-table-column label="调用" width="100" align="right">
            <template #default="{ row }">{{ formatCount(row.requests) }}</template>
          </el-table-column>
          <el-table-column label="成功率" width="110" align="right">
            <template #default="{ row }">{{ formatRatio(row.successRate) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <span :class="row.deleted ? 'text-xs text-muted' : 'text-xs'">
                {{ row.deleted ? '已删除（历史）' : '在用' }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </section>
    </template>
  </div>
</template>
