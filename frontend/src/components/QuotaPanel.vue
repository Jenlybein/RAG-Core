<script setup lang="ts">
/**
 * 今日额度：三本账（检索 / 重排 / 大模型）各还剩多少。
 *
 * **自己取数**，不接 props：额度是「今天、这个租户」的事实，与页面上的时间窗选择器无关
 * （图是滚动窗口，额度是 UTC 日桶）；让父组件传进来，就会有人在切窗口时顺手重算一次总额。
 * 复用 `CONSOLE_ME_QUERY_KEY`，与顶栏、Key 页共享同一份缓存，不额外多打一个请求。
 *
 * 五种状态各有各的画法（判定见 `utils/quota.ts`）：
 * - normal / near / out：剩余是主数字，条形按严重度变色；
 * - untracked：读数未知（限流关着）——写「未统计」、条形换成虚线空槽，绝不画成 0；
 * - off：这一档套餐不含这本账（free 档的重排 / 大模型）——写「未开通」，不画条；
 * - missing：接口没给这一栏——写「—」，不画条（也不冒充「未统计」）。
 */
import { useQuery } from '@tanstack/vue-query'
import { computed } from 'vue'

import { SERIES_ACCENT, SERIES_VIOLET, SERIES_WARM } from '../lib/charts'
import { CONSOLE_ME_QUERY_KEY, fetchMe } from '../services/console'
import type { ConsoleUsage } from '../types/console'
import { isWordState, remainingOf, showsMeter, stateOf, usedPercentOf, type QuotaState } from '../utils/quota'
import { formatCount } from '../utils/usageFormat'

const me = useQuery({
  queryKey: CONSOLE_ME_QUERY_KEY,
  queryFn: fetchMe,
  retry: false,
})

interface BookRow {
  key: string
  name: string
  /** 身份色：与趋势图图例同源（`lib/charts.ts` 的系列色） */
  color: string
  state: QuotaState
  /** 主位文字：剩余数 / 「未统计」/「未开通」 */
  value: string
  /** `/ 上限`；未开通时不显示（0 没有意义） */
  limit: string | null
  percent: number | null
  used: string | null
}

function toRow(key: string, name: string, color: string, usage: ConsoleUsage | null): BookRow {
  const state = stateOf(usage)

  if (state === 'missing' || state === 'off') {
    // 两种「没有读物」：接口没给（—）与套餐不含（未开通）。都不画条。
    return {
      key,
      name,
      color,
      state,
      value: state === 'off' ? '未开通' : '—',
      limit: null,
      percent: null,
      used: null,
    }
  }
  if (state === 'untracked') {
    return {
      key,
      name,
      color,
      state,
      value: '未统计',
      limit: usage === null ? null : formatCount(usage.limit),
      percent: null,
      used: null,
    }
  }
  return {
    key,
    name,
    color,
    state,
    value: formatCount(remainingOf(usage)),
    limit: usage === null ? null : formatCount(usage.limit),
    percent: usedPercentOf(usage),
    used: usage === null || usage.used === null ? null : `已用 ${formatCount(usage.used)}`,
  }
}

const rows = computed<BookRow[]>(() => {
  const data = me.data.value
  return [
    toRow('retrieve', '检索', SERIES_ACCENT, data?.usage ?? null),
    toRow('rerank', '重排', SERIES_WARM, data?.rerank_usage ?? null),
    toRow('llm', '大模型', SERIES_VIOLET, data?.llm_usage ?? null),
  ]
})

/** 管理员没有租户：这一块不成立，整块不渲染（而不是显示三行「未统计」）。 */
const visible = computed(() => me.isSuccess.value && me.data.value?.tenant != null)

/**
 * 原因只写在「整卡都读不到」的时候：平台级开关（`RATE_LIMIT_ENABLED=false`）一关，
 * 三本账同时未知。所以判据是「所有**存在**的账都未统计」，而不是「三行里有一行未统计」
 * ——后者在部分未知时会把原因说成整卡，在 free 档（重排 / 大模型未开通）又会误判。
 */
const untracked = computed(() => {
  const present = rows.value.filter((row) => row.state !== 'off' && row.state !== 'missing')
  return present.length > 0 && present.every((row) => row.state === 'untracked')
})
</script>

<template>
  <section v-if="visible" class="card card-body">
    <h2 class="section-title">今日额度</h2>
    <p class="mt-1 text-xs text-muted">
      {{ untracked ? '限流关闭 · ' : '' }}UTC 日界 · 本地 08:00 换天
    </p>

    <div class="quota-block mt-5">
      <div v-for="row in rows" :key="row.key" class="quota" :class="`quota--${row.state}`">
        <div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-2">
          <span class="flex items-center gap-[7px] text-[13px] text-ink">
            <i class="quota-dot" :style="{ background: row.color }" aria-hidden="true" />
            {{ row.name }}
            <span v-if="row.state === 'near'" class="quota-state">接近上限</span>
            <span v-else-if="row.state === 'out'" class="quota-state">已用尽</span>
          </span>
          <span class="flex items-baseline gap-1.5 tabular-nums">
            <span v-if="showsMeter(row.state)" class="text-xs text-muted">剩余</span>
            <span class="quota-value" :class="{ 'quota-value--word': isWordState(row.state) }">{{ row.value }}</span>
            <span v-if="row.limit !== null" class="text-xs text-muted">/ {{ row.limit }}</span>
          </span>
        </div>

        <div v-if="showsMeter(row.state)" class="mt-[7px] flex items-center gap-3">
          <div
            class="progress flex-1"
            :class="{ 'progress--unknown': row.percent === null }"
            role="progressbar"
            aria-label="已用额度占比"
            aria-valuemin="0"
            aria-valuemax="100"
            :aria-valuenow="row.percent ?? undefined"
          >
            <span v-if="row.percent !== null" :style="{ width: `${row.percent}%` }" />
          </div>
          <span v-if="row.used !== null" class="whitespace-nowrap text-xs tabular-nums text-muted">
            {{ row.used }}
          </span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/**
 * 与设计稿 `design/rag-console-replica/usage.css` 的 `.meter*` 同构：
 * 上一行名称与剩余，下一行条形与已用；条只表达消耗程度，颜色按严重度升级。
 */
.quota-block {
  /* 不铺满整行：4% 的条形放在 1120px 宽里只剩一道细线 */
  max-width: 40rem;
  display: grid;
}

.quota + .quota {
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid rgb(var(--c-line) / 0.4);
}

.quota-dot {
  width: 8px;
  height: 8px;
  border-radius: 3px;
  flex: none;
}

/**
 * 进度条（局部）：这条槽只在额度卡里用，所以不放进 main.css 变成全局组件。
 * 轨道走 `--c-line`（深色下自动反色），填充走主色，状态色由下面的 `.quota--*` 覆盖。
 */
.progress {
  height: 6px;
  border-radius: 9999px;
  background: rgb(var(--c-line) / 0.9);
  box-shadow: inset 0 1px 2px rgb(24 33 52 / 0.1);
  overflow: hidden;
}

.progress > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: rgb(var(--c-accent));
}

.quota-value {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: rgb(var(--c-ink));
}

/* 「未统计」「未开通」是词不是读数：降一档字号与字重 */
.quota-value--word {
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 0;
  color: rgb(var(--c-muted));
}

.quota-state {
  margin-left: 7px;
  font-size: 12px;
  font-weight: 500;
  color: rgb(var(--c-indexing));
}

/* 数字与条一起升级：只升一处会让人以为另一处还是正常 */
.quota--near .quota-value {
  color: rgb(var(--c-indexing));
}

.quota--near .progress > span {
  background: rgb(var(--c-star));
}

.quota--out .quota-value,
.quota--out .quota-state {
  color: rgb(var(--c-fail));
}

.quota--out .progress > span {
  background: rgb(var(--c-danger));
}

/* 未统计：空槽（这里本应有条）+ 虚线（量未知），不画 0% */
.progress--unknown {
  background: transparent;
  box-shadow: none;
  border: 1px dashed rgb(var(--c-line-strong));
  height: 7px;
}
</style>
