<script setup lang="ts">
/**
 * 档位预设页（`/admin/profiles`）：四个质量档位的参数在这里改，改完立刻对下一次检索生效。
 *
 * 五处刻意的写法：
 *
 * 1. **一行一卡，不是表格**：四行 × 四个字段的表格读起来像配置清单，而这里真正需要
 *    比较的是「档位之间的差别」——卡片并排才看得出 speed 与 quality 差在哪。
 * 2. **改名不可编辑**：档位名同时是套餐白名单（`allowed_profiles`）、前端卡片与门禁判据
 *    的词表，改一个名字要同时动三层，不是一次配置写入。
 * 3. **`custom` 单独说明**：它这一行的语义与前三个不同——只给「新建 Key 时
 *    `custom_options` 的初值」，检索时 custom 仍逐键回落部署配置。不写清楚，运营会以为
 *    改这一行能改变 custom 的检索行为。
 * 4. **保存只发动过的字段**：整行发回去会让后端把没变的列也重写一遍并写进审计，事后
 *    看账本会以为管理员逐项改过。
 * 5. **保存后提示「已生效」而不是「已保存」**：这一页的写入是即时生效的（后端在 PATCH
 *    里就刷新了注册表），说「已保存」会让人以为还要重启。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, reactive, watch } from 'vue'

import PageHeader from '../../components/PageHeader.vue'
import {
  PROFILE_PRESETS_QUERY_KEY,
  describePreset,
  fetchProfilePresets,
  patchProfilePreset,
} from '../../services/consoleProfiles'
import type { ProfilePresetData } from '../../types/profilePreset'
import { errorText } from '../../utils/errorText'

/** 一档的编辑态：四个字段都可改，`profile` 只读（见上）。 */
interface Draft {
  mode: string
  top_k: number
  rerank: boolean
  rewrite: boolean
}

const queryClient = useQueryClient()

const presetsQuery = useQuery({
  queryKey: PROFILE_PRESETS_QUERY_KEY,
  queryFn: fetchProfilePresets,
})

const rows = computed<ProfilePresetData[]>(() => presetsQuery.data.value?.items ?? [])
const loading = computed(() => presetsQuery.isPending.value)
const loadError = computed(() =>
  presetsQuery.isError.value ? errorText(presetsQuery.error.value) : '',
)

/** 草稿按档位名存：一份脏值不会因为列表重取而丢失。 */
const drafts = reactive<Record<string, Draft>>({})

watch(
  rows,
  (items) => {
    for (const item of items) {
      drafts[item.profile] = {
        mode: item.mode,
        top_k: item.top_k,
        rerank: item.rerank,
        rewrite: item.rewrite,
      }
    }
  },
  { immediate: true },
)

/** 这一档有没有改动；没有改动就不该让「保存」亮着。 */
function isDirty(item: ProfilePresetData): boolean {
  const draft = drafts[item.profile]
  if (draft === undefined) return false
  return (
    draft.mode !== item.mode ||
    draft.top_k !== item.top_k ||
    draft.rerank !== item.rerank ||
    draft.rewrite !== item.rewrite
  )
}

/** 只发动过的字段（见模块文档第 4 条）。 */
function patchBodyOf(item: ProfilePresetData): Partial<Draft> {
  const draft = drafts[item.profile]
  const body: Partial<Draft> = {}
  if (draft.mode !== item.mode) body.mode = draft.mode
  if (draft.top_k !== item.top_k) body.top_k = draft.top_k
  if (draft.rerank !== item.rerank) body.rerank = draft.rerank
  if (draft.rewrite !== item.rewrite) body.rewrite = draft.rewrite
  return body
}

const savingProfile = reactive<{ value: string | null }>({ value: null })

const saveMutation = useMutation({
  mutationFn: (item: ProfilePresetData) => patchProfilePreset(item.profile, patchBodyOf(item)),
  onSuccess: async (result, item) => {
    await queryClient.invalidateQueries({ queryKey: PROFILE_PRESETS_QUERY_KEY })
    const changed = result.changed
    ElMessage.success(
      changed.length === 0
        ? '没有改动，未提交'
        : `${item.profile} 已更新并立即生效：${changed.join('、')}`,
    )
  },
  onError: (error) => {
    ElMessage.error(errorText(error))
  },
  onSettled: () => {
    savingProfile.value = null
  },
})

function save(item: ProfilePresetData): void {
  if (!isDirty(item) || savingProfile.value !== null) return
  savingProfile.value = item.profile
  saveMutation.mutate(item)
}

/** 检索方式的候选：与后端 `SUPPORTED_RETRIEVAL_MODES` 同源，改这里要同步那边。 */
const MODE_OPTIONS = [
  { value: 'vector', label: '向量（只用语义）' },
  { value: 'hybrid', label: '混合（向量 + 关键词）' },
]

const NAME_HINTS: Record<string, string> = {
  speed: '最便宜、最可预测的一档：只走向量一路。',
  balanced: '两路召回，把关键词命中找回来；不做重排与改写。',
  quality: '链路最长：混合 + 重排 + 改写，适合「文档里的说法与我问的不一样」这类问题。',
  custom: '这一行只决定「新建 Key 时 custom 的初值」；检索时 custom 仍按 Key 上填的参数走，没填的项回落部署配置。',
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader
      title="档位预设"
      subtitle="四个质量档位的参数。改动对之后发起的每一次检索立即生效，无需重启；已经发出去的请求不受影响。"
    />

    <p v-if="loading" class="text-sm text-slate-400">正在读取档位参数…</p>
    <div
      v-else-if="loadError !== ''"
      class="rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
    >
      {{ loadError }}
    </div>

    <div v-else class="grid gap-4 lg:grid-cols-2">
      <article v-for="item in rows" :key="item.profile" class="card card-body space-y-4">
        <!-- 抬头：档位名（只读）+ 一句话差别 + 当前参数摘要 -->
        <header class="space-y-1.5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-[15px] font-semibold tracking-tight text-ink">{{ item.profile }}</h2>
            <span class="text-[11px] text-muted">当前：{{ describePreset(item) }}</span>
          </div>
          <p class="text-[12px] leading-relaxed text-muted">{{ NAME_HINTS[item.profile] ?? '' }}</p>
        </header>

        <!-- 参数：两列网格，一个字段一行，避免挤成一条 -->
        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="mb-1 block text-[11px] text-muted">检索方式</label>
            <el-select v-model="drafts[item.profile].mode" class="w-full">
              <el-option
                v-for="option in MODE_OPTIONS"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </div>

          <div>
            <label class="mb-1 block text-[11px] text-muted">返回条数</label>
            <el-input-number
              v-model="drafts[item.profile].top_k"
              :min="1"
              :max="50"
              controls-position="right"
              class="w-full"
            />
          </div>
        </div>

        <div class="grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
          <el-checkbox v-model="drafts[item.profile].rerank">结果重排</el-checkbox>
          <el-checkbox v-model="drafts[item.profile].rewrite">查询改写</el-checkbox>
        </div>

        <p class="text-[11px] leading-relaxed text-muted">
          这两项是「档位想要什么」；生效值还要与部署开关取合取——本机没装重排/改写时，
          勾上也不会跑。
        </p>

        <div class="flex items-center justify-end gap-2 border-t border-line pt-3">
          <span v-if="isDirty(item)" class="mr-auto text-[11px] text-amber-700">有未保存的改动</span>
          <el-button
            type="primary"
            size="small"
            :disabled="!isDirty(item)"
            :loading="savingProfile.value === item.profile"
            @click="save(item)"
          >
            保存
          </el-button>
        </div>
      </article>
    </div>
  </section>
</template>
