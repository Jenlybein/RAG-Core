<script setup lang="ts">
/**
 * 站点用量（管理员，第 14 步 #203）。
 *
 * 与观测页的分工：观测页看「质量」（降级、空结果、评分、六段耗时），这一页看「量」——
 * 全站用了多少、谁在拖、有没有该看一眼的事。
 *
 * 「需要看一眼」是**提示，不是处置**：阈值（额度百分比、成功率掉几个点）在「检索参数」
 * 里改，这里只把触发的那几条摆出来。没有告警时不留一块空白——空白的告警区比没有更让人不安。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import PageHeader from '../../components/PageHeader.vue'
import UsageChart from '../../components/UsageChart.vue'
import { fetchSiteUsage, fetchTenantRanking, usageQueryKey } from '../../services/usageService'
import { USAGE_WINDOWS, type UsageWindow } from '../../types/usage'
import { errorText } from '../../utils/errorText'
import { formatCount, formatMs, formatRatio, p95Label, sourceLabel } from '../../utils/usageFormat'

const router = useRouter()
const window = ref<UsageWindow>('1d')

const siteQuery = useQuery({
  queryKey: computed(() => usageQueryKey('site', window.value)),
  queryFn: () => fetchSiteUsage(window.value),
})
const rankingQuery = useQuery({
  queryKey: computed(() => usageQueryKey('tenants', window.value)),
  queryFn: () => fetchTenantRanking(window.value),
})

const site = computed(() => siteQuery.data.value ?? null)
const ranking = computed(() => rankingQuery.data.value?.tenants ?? [])
const loadError = computed(() =>
  siteQuery.isError.value ? errorText(siteQuery.error.value) : '',
)
const totals = computed(() => site.value?.totals ?? null)

const alertType = (level: string): 'error' | 'warning' | 'info' =>
  level === 'error' ? 'error' : level === 'warn' ? 'warning' : 'info'

function openTenant(tenantId: string): void {
  void router.push(`/admin/usage/${encodeURIComponent(tenantId)}`)
}
</script>

<template>
  <div class="space-y-6">
    <PageHeader title="站点用量" subtitle="全站的调用量与租户排名。">
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
          <p class="metric-label">站点调用</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatCount(totals?.requests) }}</p>
          <p class="mt-1 text-xs text-muted">失败 {{ formatCount(totals?.failed) }} 次</p>
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
          <p class="metric-label">{{ p95Label(site?.p95Source ?? 'exact') }}</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatMs(totals?.p95Ms) }}</p>
        </div>
      </div>

      <section v-if="(site?.alerts.length ?? 0) > 0" class="card card-body space-y-2">
        <h2 class="section-title">需要看一眼</h2>
        <el-alert
          v-for="alert in site?.alerts ?? []"
          :key="alert.kind + alert.message"
          :type="alertType(alert.level)"
          :title="alert.message"
          :closable="false"
          show-icon
        />
      </section>

      <section class="card card-body">
        <div class="flex items-baseline justify-between gap-2">
          <h2 class="section-title">全站趋势</h2>
          <span class="text-xs text-muted">{{ sourceLabel(site?.source ?? 'events') }} · 最后一桶未走完</span>
        </div>
        <div v-if="(site?.buckets.length ?? 0) === 0" class="py-10 text-center text-sm text-muted">
          这个窗口内全站没有调用记录。
        </div>
        <UsageChart v-else :buckets="site?.buckets ?? []" kind="stacked" :height="240" />
      </section>

      <section class="card">
        <div class="card-body flex items-baseline justify-between gap-2">
          <h2 class="section-title">租户排名</h2>
          <span class="text-xs text-muted">点一行下钻</span>
        </div>
        <el-table
          :data="ranking"
          size="small"
          empty-text="这个窗口内没有租户调用过"
          @row-click="(row: { tenantId: string }) => openTenant(row.tenantId)"
        >
          <el-table-column prop="tenantId" label="租户" min-width="160" />
          <el-table-column label="调用" width="100" align="right">
            <template #default="{ row }">{{ formatCount(row.requests) }}</template>
          </el-table-column>
          <el-table-column label="成功率" width="110" align="right">
            <template #default="{ row }">{{ formatRatio(row.successRate) }}</template>
          </el-table-column>
          <el-table-column label="P95" width="110" align="right">
            <template #default="{ row }">{{ formatMs(row.p95Ms) }}</template>
          </el-table-column>
        </el-table>
      </section>
    </template>
  </div>
</template>
