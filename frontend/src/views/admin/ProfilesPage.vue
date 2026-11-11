<script setup lang="ts">
/**
 * 档位预设页（`/admin/profiles`）：四个质量档位的参数在这里改，改完立刻对下一次检索生效。
 *
 * 六处刻意的写法：
 *
 * 1. **一行一卡，不是表格**：四行 × 四个字段的表格读起来像配置清单，而这里真正需要
 *    比较的是「档位之间的差别」——卡片并排才看得出 speed 与 quality 差在哪。
 * 2. **共用说明只写一次，在页头**：重排/改写是「档位想要什么」这件事对四张卡完全一样，
 *    贴在每张卡里就是同一句话重复四遍，把卡片撑高、把差异淹掉。
 * 3. **不摆「当前：…」摘要**：控件本身已经显示当前值（下拉显示方式、数字框显示条数、
 *    勾选框显示开关），再抄一句成摘要只是把同样的信息说第二遍。
 * 4. **保存只在有改动时出现**：四颗常驻的灰色禁用按钮是纯噪音，它们不告诉用户任何
 *    信息——「没改动」这件事不需要一个按钮来表达。
 * 5. **改名不可编辑**：档位名同时是套餐白名单（`allowed_profiles`）、前端卡片与门禁判据
 *    的词表，改一个名字要同时动三层，不是一次配置写入。
 * 6. **`custom` 单独说明**：它这一行的语义与前三个不同——只给「新建 Key 时
 *    `custom_options` 的初值」，检索时 custom 仍逐键回落部署配置。
 *
 * 保存只发动过的字段：整行发回去会让后端把没变的列也重写一遍并写进审计，事后看账本
 * 会以为管理员逐项改过。成功的提示是「已更新并立即生效」而不是「已保存」——这一页的
 * 写入是即时生效的，说「已保存」会让人以为还要重启。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, reactive, watch } from 'vue'

import PageHeader from '../../components/PageHeader.vue'
import { CONSOLE_ME_QUERY_KEY } from '../../services/console'
import { PROFILE_PRESETS_QUERY_KEY, fetchProfilePresets, patchProfilePreset } from '../../services/consoleProfiles'
import type { ProfilePresetData } from '../../types/profilePreset'
import { errorText } from '../../utils/errorText'

/** 一档的编辑态：四个字段可改，`profile` 只读（见模块文档第 5 条）。 */
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

/** 只发动过的字段（见模块文档末段）。 */
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
    // `me` 里也带着一份 `profile_presets`：Key 编辑器的档位悬浮说明读的是它。
    // 只失效上面那一个 key 的话，管理员改完、切到 /keys 看到的还是旧参数——
    // 而「改完即时生效」正是这一页承诺的唯一一件事。
    await queryClient.invalidateQueries({ queryKey: CONSOLE_ME_QUERY_KEY })
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

/** 一句话说清这一档的取舍。**只写差别，不写参数**——参数在下面的控件里。 */
const NAME_HINTS: Record<string, string> = {
  speed: '只走向量一路：最便宜，也最可预测。',
  balanced: '把关键词命中一并召回，不做重排与改写。',
  quality: '链路最长：混合召回 + 重排 + 改写。',
  custom: '只决定新建 Key 时 custom 档的初值；检索时仍按 Key 上填的参数走，没填的项回落部署配置。',
}
</script>

<template>
  <section class="space-y-6">
    <PageHeader
      title="档位预设"
      subtitle="四个质量档位的参数，改动对之后发起的检索生效。"
    />

    <!--
      两条共用说明都只在这里出现一次：
      ① 重排/改写是「想要」，生效还要看本机装没装；② 档位名与「能加一档」的边界。
      （第 ② 条尤其容易被当成缺陷来报：「为什么不能新建档位」的答案在这里。）
    -->
    <div class="card card-body space-y-1.5">
      <p class="text-[12px] leading-relaxed text-muted">
        「结果重排」和「查询改写」记的是<b class="font-medium text-ink">档位想要什么</b>；
        实际会不会跑还要看本机有没有装这两项能力——没装时勾上也不会执行。
      </p>
      <p class="text-[12px] leading-relaxed text-muted">
        档位名不可改，也不能新增：它同时是套餐白名单、租户面板的档位卡片与门禁判据的词表，
        加一档要同时动这三层。
      </p>
    </div>

    <p v-if="loading" class="text-sm text-slate-400">正在读取档位参数…</p>
    <div
      v-else-if="loadError !== ''"
      class="rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
    >
      {{ loadError }}
    </div>

    <div v-else class="grid items-start gap-4 lg:grid-cols-2">
      <article v-for="item in rows" :key="item.profile" class="card card-body space-y-4">
        <header class="flex items-start justify-between gap-3">
          <div class="min-w-0 space-y-1">
            <h2 class="text-[15px] font-semibold tracking-tight text-ink">{{ item.profile }}</h2>
            <p class="text-[12px] leading-relaxed text-muted">
              {{ NAME_HINTS[item.profile] ?? '' }}
            </p>
          </div>
          <!-- 只在有改动时出现：常驻的灰色禁用按钮不传达任何信息（见模块文档第 4 条）。 -->
          <el-button
            v-if="isDirty(item)"
            type="primary"
            size="small"
            class="shrink-0"
            :loading="savingProfile.value === item.profile"
            @click="save(item)"
          >
            保存
          </el-button>
        </header>

        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="mb-1 block text-xs text-muted">检索方式</label>
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
            <label class="mb-1 block text-xs text-muted">返回条数</label>
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
      </article>
    </div>
  </section>
</template>
