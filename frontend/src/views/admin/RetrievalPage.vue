<script setup lang="ts">
/**
 * 检索参数页：按后端返回的三档（不可迁移 / 需重启 / 可热生效）渲染白名单里的全部运行时
 * 配置，**只允许改可热生效的那些**。
 *
 * 七处刻意的写法：
 *
 * 1. **分组顺序照后端**：`GET /console/settings` 已按 locked → restart → hot 排好，并且
 *    每档都返回（哪怕空组）。这里用 `groupByTier` 重新按行归组，只是为了不渲染空标题，
 *    顺序仍是后端给的首次出现顺序，前端**不排第二次**。
 * 2. **每行都展示 `why`**：那句「为什么在这一档」由服务端白名单写好（`SETTING_SPECS`），
 *    页面不再自己编理由——编一份就会与后端的写入边界漂移。
 * 3. **保存是两步**：先 `PATCH ?dry_run=true` 拿影响面，有影响才 `ElMessageBox` 确认，
 *    确认后才 `?dry_run=false` 真写。影响面的数字一律用 `count`（`sample` 最多 5 个名字）。
 *    这段顺序由 `composables/useDryRunSave.ts` 统一实现；确认判定与文案仍在页面。
 * 4. **保存后的提示按档位分开**：热更写「已推送（各进程 Ns 内生效）」，N 来自后端的
 *    `cache_ttl_seconds`（不写死 30）；需重启写「已保存，待重启生效」。不写「立即生效」。
 * 5. **非法行标红**：`invalid=true` 表示库里那一行读时校验失败、运行期已回退到 `.env`
 *    值；页面显示 `invalid_reason` 与「已回退到 .env 值」，管理员据此去修那一行。
 * 6. **角色指针行在这里也能改**：它们是热更键，保存走同一条 PATCH；但更顺手的方式是
 *    到模型配置页用下拉改指向，所以输入框旁给了一条链接与说明。
 * 7. **需重启档在面板上只读**：#93/#96 的验收都要求「需重启重建档在面板上不可编辑」。
 *    这一档不渲染任何输入控件，只显示当前值与来源；`source === 'db'` 的行标
 *    「已写入库，待重启生效」，要改请走运维流程。保存时因此只提交 hot 档的改动，
 *    没有 hot 改动时保存按钮禁用。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, h, reactive, ref, watch } from 'vue'

import { useDryRunSave } from '../../composables/useDryRunSave'
import {
  SETTINGS_QUERY_KEY,
  fetchSettings,
  updateSettings,
} from '../../services/consoleSettings'
import type {
  SettingRow,
  SettingValue,
  SettingsImpact,
} from '../../types/runtimeSettings'
import { errorText } from '../../utils/errorText'
import { impactLines, shouldConfirmImpact } from '../../utils/impactPreview'
import { MODEL_VENDORS } from '../../utils/modelsPage'
import {
  groupByTier,
  isEditable,
  isRolePointerKey,
  kindOf,
  validateHotValue,
} from '../../utils/settingsTier'

const queryClient = useQueryClient()

const settingsQuery = useQuery({
  queryKey: SETTINGS_QUERY_KEY,
  queryFn: fetchSettings,
})

const cacheTtl = computed(() => settingsQuery.data.value?.cache_ttl_seconds ?? 30)
const loading = computed(() => settingsQuery.isPending.value)
const loadError = computed(() =>
  settingsQuery.isError.value ? errorText(settingsQuery.error.value) : '',
)

/**
 * 组顺序照后端、空组不渲染。
 *
 * 后端同时给了每组的 `title`（中文小标题）；这里按 tier 取回它，而不是在前端再写一张
 * 「locked 叫什么」的表。
 */
const groups = computed(() => {
  const data = settingsQuery.data.value
  if (data === undefined) return []

  const metaByTier = new Map(data.groups.map((group) => [group.tier, group]))
  return groupByTier(data.groups.flatMap((group) => group.settings)).map((group) => {
    const meta = metaByTier.get(group.tier)
    return {
      tier: group.tier,
      title: meta?.title ?? group.tier,
      rows: group.rows,
    }
  })
})

/**
 * 各类型的草稿分开存：布尔给 `el-switch`、数字给 `el-input-number`、其余给文本/下拉。
 *
 * 合成一个 `Record<string, unknown>` 会让模板里的 `v-model` 失去类型，而 Element Plus
 * 的受控组件对 `null` / `undefined` 的容忍各不相同；按类型分开最省心。
 */
const textDrafts = reactive<Record<string, string>>({})
const numberDrafts = reactive<Record<string, number>>({})
const boolDrafts = reactive<Record<string, boolean>>({})

/**
 * 个别键的取值域来自后端的常量（不是从响应里能推出来的）：渲染成下拉，省得管理员敲错
 * 一个拼写让下一次调用才报错。不在表里的字符串仍然是输入框——后端可能有本页不知道的
 * 扩展点，硬做成下拉会把它们锁死。
 */
const VALUE_DOMAINS: Record<string, readonly string[]> = {
  RETRIEVAL_MODE: ['vector', 'bm25', 'hybrid'],
  LLM_PROVIDER: MODEL_VENDORS,
  KEYWORD_SEARCH_PROVIDER: ['none', 'elasticsearch'],
}

function domainOf(row: SettingRow): readonly string[] | null {
  return VALUE_DOMAINS[row.key] ?? null
}

/** 把服务端的有效值灌进草稿；保存后重新读取时也走这里，输入框跟着新值复位。 */
function resetDrafts(rows: readonly SettingRow[]): void {
  for (const row of rows) {
    if (!isEditable(row)) continue
    if (typeof row.value === 'boolean') {
      boolDrafts[row.key] = row.value
    } else if (typeof row.value === 'number') {
      numberDrafts[row.key] = row.value
    } else {
      textDrafts[row.key] = row.value === null ? '' : String(row.value)
    }
  }
}

watch(
  groups,
  (current) => {
    resetDrafts(current.flatMap((group) => group.rows))
  },
  { immediate: true },
)

/** 这一行现在要提交的值。 */
function inputValueOf(row: SettingRow): SettingValue {
  const kind = kindOf(row)
  if (kind === 'boolean') return boolDrafts[row.key] ?? false
  // 输入框被清空时 `el-input-number` 给的是 null/undefined；转成 NaN 让校验报「请输入有效
  // 数字」，而不是悄悄按 0 保存下去。
  if (kind === 'number') return numberDrafts[row.key] ?? Number.NaN
  return textDrafts[row.key] ?? ''
}

/**
 * 有效值与草稿是否语义相同。
 *
 * 角色指针的 `null` 与空串是同一个状态（「不指向」），不能当成一次改动；其余类型直接
 * 比较（数字不做字符串化，免得 `"5"` 与 `5` 被当成同一次修改）。
 */
function sameValue(row: SettingRow, next: SettingValue): boolean {
  if (isRolePointerKey(row.key)) {
    const current = row.value === null ? '' : String(row.value)
    return current === next
  }
  return row.value === next
}

/**
 * 这次提交的差异：只发真动过的 **hot** 行，没动的键不写库也不进审计。
 *
 * `isEditable` 已经把 locked 与 restart 排除在外，所以这里天然只收集热更档：
 * restart 档在面板上只读，改它要走运维流程。
 */
const changes = computed<Record<string, SettingValue>>(() => {
  const values: Record<string, SettingValue> = {}
  for (const group of groups.value) {
    for (const row of group.rows) {
      if (!isEditable(row)) continue
      const next = inputValueOf(row)
      if (!sameValue(row, next)) {
        values[row.key] = next
      }
    }
  }
  return values
})

/** 只有 hot 档有改动时才允许保存；按钮的禁用条件就是它。 */
const dirty = computed(() => Object.keys(changes.value).length > 0)

/**
 * 两步保存走通用组合式函数：试算 → （有影响才）确认 → 真写。
 *
 * 判定与文案仍留在本页：`SettingsImpact`（档位少一步能力 / 失去关键词通道）与套餐页的
 * `PlanImpact` 形状不同，只有本页知道哪些影响面值得停下来问。组合式函数负责顺序与 loading。
 */
const { saving, save: runDryRunSave } = useDryRunSave<SettingsImpact>({
  formatImpact: (impact) => {
    if (!shouldConfirmImpact(impact)) return null
    return {
      title: '保存前确认影响面',
      message: h('div', { class: 'text-sm leading-6' }, [
        ...impactLines(impact).map((line) => h('p', line)),
        h('p', { class: 'mt-2 text-slate-500' }, '确认后才会真正落库；取消不会写入。'),
      ]),
    }
  },
})

function formatValue(row: SettingRow): string {
  if (row.value === null) return '不指向（回落 .env）'
  if (typeof row.value === 'boolean') return row.value ? '开' : '关'
  if (row.value === '') return '（空）'
  return String(row.value)
}

// —— 折叠 ——
//
// 三组加起来五十多项，一屏放不下；而日常真正要动的只有「热生效」那一组，另外两组是
// 查证用的（只读 / 待重启）。默认只展开能改的那一组，另外两组收起。
const openGroups = ref<string[]>(['hot'])

/** 全部展开 / 收起：五十行的清单里，逐个点开三组很烦。 */
const allOpen = computed(() => groups.value.length > 0 && openGroups.value.length === groups.value.length)

function toggleAll(): void {
  openGroups.value = allOpen.value ? [] : groups.value.map((group) => group.tier)
}

/**
 * 这一组有几项「不对劲」。
 *
 * 收起之后异常会被一起藏起来，所以标题上必须留个数字：`invalid`（库里那一行被丢弃、
 * 已回退到 .env）和「已写入库、待重启生效」都属于「页面显示的值不是进程正在用的值」，
 * 恰恰是最该被看到的一类。
 */
function alertCountOf(group: { tier: string; rows: readonly SettingRow[] }): number {
  return group.rows.filter(
    (row) => row.invalid || (group.tier === 'restart' && row.source === 'db'),
  ).length
}

/**
 * 档位徽章配色。暖色令牌，与 StatusBadge / statusPill 同一组语义色：
 * 热生效=成功绿、待重启=警示黄、只读=中性。
 *
 * 这三组颜色原来还是重构前的冷色（#D1FAE5 / #FEF3C7 / #F3F4F6），压在这套米白上
 * 像贴上去的标签；换成暖色后与卡片底同色系。
 */
function tierBadgeClass(tier: string): string {
  if (tier === 'hot') return 'bg-[#E4EDDD] text-[#3C5228]'
  if (tier === 'restart') return 'bg-[#F7E9C9] text-[#6E4E12]'
  return 'bg-[#F0ECE4] text-[#6B6558]'
}

function tierBadgeText(tier: string): string {
  if (tier === 'hot') return '热生效'
  if (tier === 'restart') return '待重启'
  return '只读'
}

/** 某一档的说明：页头那句总说明之外，每档还要写清楚它为什么这样。 */
function tierHint(tier: string): string {
  if (tier === 'locked') {
    return '改了等于换基础设施或能关掉鉴权：面板不提供入口，只能由部署方改 .env。'
  }
  if (tier === 'restart') {
    return (
      '面板不提供写入口：要改请走运维流程（重启 + 数据重建后生效）。' +
      '已写入库的值会显示出来，但运行中的进程仍按旧值跑。'
    )
  }
  return `可热改：保存后各进程最多 ${cacheTtl.value}s 内收敛，下一次检索即按新值跑。`
}

/**
 * 保存：校验 → 两步保存（试算 → 有影响才确认 → 真写）。
 *
 * `values` 只含 hot 档的改动（`changes` 已排除 restart / locked）。两步顺序与 loading
 * 交给 `useDryRunSave`；本页只负责逐行校验、影响面文案，以及成功后的提示与缓存刷新。
 */
async function save(): Promise<void> {
  const values = { ...changes.value }
  const keys = Object.keys(values)
  if (keys.length === 0) {
    ElMessage.info('没有改动，未发送请求')
    return
  }

  // 逐行校验：错误信息带上键名，长清单里才知道是哪一行。
  for (const group of groups.value) {
    for (const row of group.rows) {
      if (!(row.key in values)) continue
      const check = validateHotValue(row, values[row.key])
      if (!check.ok) {
        ElMessage.warning(`${row.key}：${check.message}`)
        return
      }
      values[row.key] = check.value
    }
  }

  // 把这一轮点保存时的 values 捕进闭包里：试算与真写必须是同一份改动，
  // 不能因为用户在请求途中又改了某个输入框而悄悄换成另一份。
  const outcome = await runDryRunSave((dryRun) => updateSettings(values, dryRun))
  if (outcome.status === 'failed') {
    ElMessage.error(errorText(outcome.error))
    return
  }
  if (outcome.status === 'cancelled') return

  const result = outcome.result
  notifySaved(result.hot.length > 0, result.restart.length > 0, result.note)
  await queryClient.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY })
}

/**
 * 保存成功的提示必须区分两档。热更的「Ns」用后端返回的 `cache_ttl_seconds`，
 * 不写死 30：TTL 改过之后，页面上的承诺要跟着后端变。
 */
function notifySaved(hasHot: boolean, hasRestart: boolean, note: string): void {
  const parts: string[] = []
  if (hasHot) parts.push(`已推送（各进程 ${cacheTtl.value}s 内生效）`)
  if (hasRestart) parts.push('已保存，待重启生效')
  ElMessage.success(parts.length > 0 ? parts.join('；') : note || '已保存')
}

async function refresh(): Promise<void> {
  await settingsQuery.refetch()
}
</script>

<template>
  <section class="card card-body">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="page-title">检索参数</h2>
        <p class="page-subtitle">
          按「改了之后要做什么」分三档：<b class="font-medium text-ink">只读</b>改了等于换基础设施或能关掉鉴权，
          <b class="font-medium text-ink">待重启</b>只显示当前值、要改走运维流程，
          <b class="font-medium text-ink">热生效</b>保存后各进程 {{ cacheTtl }}s 内收敛。
        </p>
        <p class="mt-1 text-[11px] text-muted">
          每行「为什么在这一档」由服务端的配置白名单给出，本页不自己编理由。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <el-button v-if="groups.length > 1" size="small" @click="toggleAll">
          {{ allOpen ? '全部收起' : '全部展开' }}
        </el-button>
        <el-button size="small" :loading="settingsQuery.isFetching.value" @click="refresh">
          刷新
        </el-button>
        <!-- 只在有改动时出现：常驻的灰色禁用按钮不传达任何信息。 -->
        <el-button v-if="dirty" type="primary" size="small" :loading="saving" @click="save">
          保存改动
        </el-button>
      </div>
    </div>

    <div v-if="loading" class="py-10 text-center text-sm text-slate-500">加载检索参数…</div>

    <div
      v-else-if="loadError !== ''"
      class="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      <p>检索参数读取失败：{{ loadError }}</p>
      <el-button class="mt-2" size="small" @click="refresh">重试</el-button>
    </div>

    <!--
      三组可折叠。默认只展开「热生效」——日常真正会动的只有那一组（另外两组是查证用的
      只读 / 待重启），五十多行一屏放不下时，把不动的收起来比让人一直滚更合理。

      收起态的标题必须自带状态：组名、档位徽章、条目数，以及**异常计数**——收起来之后
      「库里那一行被丢弃」和「已写入库、待重启生效」会被一起藏掉，而它们恰恰是「屏幕上的
      值不是进程正在用的值」这类最该被看到的信息。
    -->
    <div v-else class="mt-6">
      <el-collapse v-model="openGroups">
        <el-collapse-item v-for="group in groups" :key="group.tier" :name="group.tier">
          <template #title>
            <div class="flex flex-wrap items-center gap-2.5 py-1">
              <span class="text-[14px] font-semibold tracking-tight text-ink">
                {{ group.title }}
              </span>
              <span
                class="rounded-full px-2 py-0.5 text-[11px]"
                :class="tierBadgeClass(group.tier)"
              >
                {{ tierBadgeText(group.tier) }}
              </span>
              <span class="text-[11px] text-muted">{{ group.rows.length }} 项</span>
              <span
                v-if="alertCountOf(group) > 0"
                class="rounded-full bg-[#F7E2DD] px-2 py-0.5 text-[11px] text-[#8C2F22]"
              >
                {{ alertCountOf(group) }} 项需注意
              </span>
            </div>
          </template>

          <p class="text-[11px] leading-relaxed text-muted">{{ tierHint(group.tier) }}</p>

          <div class="mt-3 overflow-x-auto">
            <!--
              四列而不是五列：原来还有一列「状态」（只读 / 面板只读 / 可热改），而分区标题
              已经说明了这件事——这一列永远是同一个值，重复了一遍又挤掉了右边。异常信息
              （invalid、待重启）挪到「来源」列下面，它们本来就与来源同源。
            -->
            <table class="min-w-full text-sm">
            <thead>
              <tr>
                <th class="th-label px-3 py-2 text-left">配置项</th>
                <th class="th-label px-3 py-2 text-left">有效值</th>
                <th class="th-label px-3 py-2 text-left">来源</th>
                <th class="th-label w-[38%] px-3 py-2 text-left">为什么在这一档</th>
              </tr>
            </thead>
            <tbody>
              <!-- 标红的行整行用「失败」底色：库里那一行已被读时丢弃，现在跑的是 .env 值。 -->
              <tr
                v-for="row in group.rows"
                :key="row.key"
                class="border-t border-line align-top"
                :class="row.invalid ? 'bg-[#FDF0EC]' : ''"
              >
                <td class="px-3 py-3">
                  <p class="font-mono text-[12px] font-medium text-ink">{{ row.key }}</p>
                  <p v-if="isRolePointerKey(row.key)" class="mt-1 text-[11px] text-muted">
                    角色指向；也能去
                    <RouterLink to="/admin/models" class="underline hover:text-accent">
                      模型配置页
                    </RouterLink>
                    用下拉改
                  </p>
                </td>

                <td class="max-w-[22rem] px-3 py-3">
                  <!-- 可写的行渲染输入控件；只读行显示后端给的有效值。 -->
                  <template v-if="isEditable(row)">
                    <el-checkbox v-if="kindOf(row) === 'boolean'" v-model="boolDrafts[row.key]" />
                    <el-input-number
                      v-else-if="kindOf(row) === 'number'"
                      v-model="numberDrafts[row.key]"
                      controls-position="right"
                      style="width: 140px"
                    />
                    <el-select
                      v-else-if="domainOf(row) !== null"
                      v-model="textDrafts[row.key]"
                      style="width: 180px"
                    >
                      <el-option
                        v-for="option in domainOf(row) ?? []"
                        :key="option"
                        :value="option"
                        :label="option"
                      />
                    </el-select>
                    <el-input
                      v-else
                      v-model="textDrafts[row.key]"
                      style="max-width: 260px"
                      :placeholder="
                        isRolePointerKey(row.key) ? '留空 = 不指向（回落 .env）' : ''
                      "
                    />
                  </template>
                  <!-- 只读值：连接串很长，限宽 + break-all，别把「为什么」那列挤没。 -->
                  <span
                    v-else
                    class="break-all font-mono text-[11px] leading-relaxed text-slate-600"
                  >
                    {{ formatValue(row) }}
                  </span>

                  <!-- 角色指向显示的是 id；给一行小字说明它是什么，避免被当成乱码。 -->
                  <p v-if="isRolePointerKey(row.key)" class="mt-1 font-mono text-[11px] text-muted">
                    当前：{{ row.value === null || row.value === '' ? '不指向' : row.value }}
                  </p>
                </td>

                <td class="px-3 py-3 text-[11px] whitespace-nowrap">
                  <span v-if="row.source === 'db'" class="font-medium text-ink">已被面板覆盖</span>
                  <span v-else class="text-muted">来自 .env</span>
                  <!-- restart 档不许在面板上改，但库里可能已有旧写入：标出来是「待生效」，
                       否则这里看起来与 .env 值一样，没人知道它在等一次重启。 -->
                  <span
                    v-if="group.tier === 'restart' && row.source === 'db'"
                    class="mt-1 block rounded bg-[#F7E9C9] px-1.5 py-0.5 text-[#6E4E12]"
                  >
                    已写入库，待重启生效
                  </span>
                  <span v-if="row.invalid" class="mt-1 block rounded bg-[#F7E2DD] px-1.5 py-0.5 text-[#8C2F22]">
                    库里那一行被丢弃：{{ row.invalid_reason }}；已回退到 .env 值
                  </span>
                </td>

                <td class="px-3 py-3 text-[11px] leading-relaxed text-muted">{{ row.why }}</td>
              </tr>
            </tbody>
          </table>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>
  </section>
</template>
