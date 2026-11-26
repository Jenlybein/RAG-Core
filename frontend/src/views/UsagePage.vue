<script setup lang="ts">
/**
 * 用量（租户，第 14 步 #203）。
 *
 * 一屏回答四件事：**这段时间用了多少、成功率如何、慢在哪、哪把 Key 在拖**。
 *
 * 三条显示纪律：
 *
 * - 成功率、P50、P95 为 `null` 时显示「—」而不是 0——空窗口不是「0% 成功」；
 * - `unknownCode > 0` 时在成功率下面明说「其中 N 次未统计」，因为那些行没有业务码，
 *   它们进调用次数、不进成功率的分母；
 * - 长窗（7/30 天）的 P95 来自日汇总直方图，标题会写「近似」。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import PageHeader from '../components/PageHeader.vue'
import QuotaPanel from '../components/QuotaPanel.vue'
import UsageChart from '../components/UsageChart.vue'
import { fetchTenantUsage, usageQueryKey } from '../services/usageService'
import { USAGE_WINDOWS, type UsageWindow } from '../types/usage'
import { errorText } from '../utils/errorText'
import { formatCount, formatMs, formatRatio, p95Label, sourceLabel } from '../utils/usageFormat'

const router = useRouter()
const window = ref<UsageWindow>('1d')

const query = useQuery({
  queryKey: computed(() => usageQueryKey('tenant', window.value)),
  queryFn: () => fetchTenantUsage(window.value),
})

const data = computed(() => query.data.value ?? null)
const loadError = computed(() => (query.isError.value ? errorText(query.error.value) : ''))

const kpis = computed(() => {
  const totals = data.value?.totals
  return {
    requests: totals?.requests ?? null,
    successRate: totals?.successRate ?? null,
    p50: totals?.p50Ms ?? null,
    p95: totals?.p95Ms ?? null,
    unknown: totals?.unknownCode ?? 0,
    failed: totals?.failed ?? 0,
    meters: [totals?.meterR ?? 0, totals?.meterRr ?? 0, totals?.meterLlm ?? 0] as const,
  }
})

function openKey(keyId: string): void {
  if (keyId === '') return
  void router.push(`/usage/keys/${encodeURIComponent(keyId)}`)
}
</script>

<template>
  <div class="space-y-6">
    <PageHeader title="用量" subtitle="这个租户的调用、成功率与每一把 Key 的账。">
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
          <p class="metric-value mt-1 text-[26px]">{{ formatCount(kpis.requests) }}</p>
          <p class="mt-1 text-xs text-muted">失败 {{ formatCount(kpis.failed) }} 次</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">成功率</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatRatio(kpis.successRate) }}</p>
          <p class="mt-1 text-xs text-muted">
            <template v-if="kpis.unknown > 0">其中 {{ formatCount(kpis.unknown) }} 次未统计</template>
            <template v-else>分母只算结局已知的请求</template>
          </p>
        </div>
        <div class="card card-body">
          <p class="metric-label">P50 耗时</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatMs(kpis.p50) }}</p>
        </div>
        <div class="card card-body">
          <p class="metric-label">{{ p95Label(data?.p95Source ?? 'exact') }}</p>
          <p class="metric-value mt-1 text-[26px]">{{ formatMs(kpis.p95) }}</p>
          <p class="mt-1 text-xs text-muted">三本账 {{ kpis.meters[0] }} / {{ kpis.meters[1] }} / {{ kpis.meters[2] }}</p>
        </div>
      </div>

      <section class="card card-body">
        <div class="flex items-baseline justify-between gap-2">
          <h2 class="section-title">三本账</h2>
          <span class="text-xs text-muted">{{ sourceLabel(data?.source ?? 'events') }} · 最后一桶未走完</span>
        </div>
        <div v-if="(data?.buckets.length ?? 0) === 0" class="py-10 text-center text-sm text-muted">
          这个窗口内还没有调用记录。
        </div>
        <UsageChart v-else :buckets="data?.buckets ?? []" kind="stacked" :height="240" />
      </section>

      <section class="card card-body">
        <h2 class="section-title">成功率与 P95</h2>
        <p class="mt-1 text-xs text-muted">左轴成功率（%），右轴 P95（ms）。没有样本的桶不连线。</p>
        <UsageChart
          v-if="(data?.buckets.length ?? 0) > 0"
          :buckets="data?.buckets ?? []"
          kind="quality"
          :height="200"
        />
      </section>

      <!--
        今日额度放在图之后、按 Key 之前：它回答的是「我还能跑多少」，与上面的窗口内统计
        是两套口径（额度按 UTC 日切，图是滚动窗口）。放在页面顶端会让两块数字被当成同一件事。
        它自己取 `/console/me`，不随时间窗重查。
      -->
      <QuotaPanel />

      <section class="card">
        <div class="card-body flex items-baseline justify-between gap-2">
          <h2 class="section-title">按 Key</h2>
          <span class="text-xs text-muted">点一行看单把 Key 的账</span>
        </div>
        <el-table
          :data="data?.keys ?? []"
          size="small"
          empty-text="这个窗口内没有 Key 调用过"
          @row-click="(row: { keyId: string }) => openKey(row.keyId)"
        >
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
          <el-table-column label="状态" width="100">
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
